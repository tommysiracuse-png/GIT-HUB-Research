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

try:
    from storage import RUNS_DIR
except ModuleNotFoundError:  # pragma: no cover - fallback for isolated test imports
    from src.storage import RUNS_DIR


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
CUSTOM_ROUTES_PATH = CONFIG_DIR / "execution_routes.json"
EXAMPLE_ROUTES_PATH = CONFIG_DIR / "execution_routes.example.json"
REPORT_JSON = RUNS_DIR / "route_resolver_report.json"
REPORT_MD = RUNS_DIR / "route_resolver_report.md"
ROUTE_INTELLIGENCE_JSON = RUNS_DIR / "route_intelligence_report.json"
ROUTE_INTELLIGENCE_MD = RUNS_DIR / "route_intelligence_report.md"

REQUIREMENT_STATUSES = {"confirmed", "missing", "unknown", "not_applicable"}
HARD_BLOCKING_LEVELS = {"hard", "blocking"}
ROUTE_STATUSES = {"standard", "conditional", "route_unknown", "blocked", "unsupported_or_unknown"}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_route_registry() -> dict:
    path = CUSTOM_ROUTES_PATH if CUSTOM_ROUTES_PATH.exists() else EXAMPLE_ROUTES_PATH
    if not path.exists():
        return {"routes": []}
    return json.loads(path.read_text(encoding="utf-8"))


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

    if venue == "OKX" and trade_type == "perp_funding_basis":
        standard_perp = direction in {
            "short_perp_long_spot",
            "basis_mean_reversion_short_perp",
            "funding_capture_short_perp",
        }
        required = ["crypto_derivatives"]
        missing = [] if caps.get("crypto_derivatives", False) else ["crypto_derivatives"]
        route_id = "okx_derivatives_paper"
        borrow_required = False
        margin_required = True
        notes = ["OKX perpetual leg can be paper-tested through the derivatives route."]
        if not standard_perp:
            route_id = "conditional_crypto_route_paper"
            borrow_required = True
            margin_required = True
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

    if venue == "YAHOO_PROXY" or trade_type in {"global_proxy_momentum", "global_market_discovery_proxy"}:
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
        allowed = bool(caps.get("prediction_markets", False))
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
            route_notes=["Event-contract execution requires account, jurisdiction, contract eligibility, and API checks."],
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


def enrich_candidate_with_route(candidate: dict, settings: dict, registry: dict | None = None) -> dict:
    enriched = dict(candidate)
    route = resolve_candidate_route(enriched, settings, registry=registry)
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
            "route_notes": route["route_notes"],
            "borrow_required": route["borrow_required"],
            "borrow_status": route["borrow_status"],
            "margin_required": route["margin_required"],
            "api_access_status": route["api_access_status"],
            "fee_model_status": route["fee_model_status"],
            "market_hours_status": route["market_hours_status"],
        }
    )
    enriched["execution_feasibility"] = existing
    enriched["execution_route"] = route
    enriched["route_id"] = route["route_id"]
    enriched["route_status"] = route["route_status"]
    return enriched


def enrich_candidates(candidates: Iterable[dict], settings: dict) -> list[dict]:
    registry = load_route_registry()
    return [enrich_candidate_with_route(candidate, settings, registry=registry) for candidate in candidates]


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
        score = float(candidate.get("score") or 0.0)
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


def _route_intelligence_markdown(report: dict) -> str:
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
    return "\n".join(lines) + "\n"


def write_route_resolver_report(candidates: list[dict], settings: dict, limit: int = 250) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    considered = candidates[:limit]
    summary = summarize_routes(considered)
    report = {
        "generated_at": _utc_now(),
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": summary,
        "route_intelligence": summarize_route_intelligence(considered),
        "routes_registry": str(CUSTOM_ROUTES_PATH if CUSTOM_ROUTES_PATH.exists() else EXAMPLE_ROUTES_PATH),
        "hard_limits": [
            "Read-only resolver; no broker API actions are performed.",
            "Conditional and route_unknown ideas remain paper-only.",
            "Live execution is still blocked unless the global live-trading gates are explicitly enabled elsewhere.",
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    ROUTE_INTELLIGENCE_JSON.write_text(json.dumps(report["route_intelligence"], indent=2), encoding="utf-8")
    ROUTE_INTELLIGENCE_MD.write_text(_route_intelligence_markdown(report["route_intelligence"]), encoding="utf-8")
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
