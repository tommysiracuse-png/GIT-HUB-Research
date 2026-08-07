"""Fail-closed contract and accounting for direct paired paper labels.

This module is deliberately independent of scanners, execution, collectors,
and storage.  Every producer and consumer can therefore validate the same
schema without creating import cycles or silently interpreting legacy fields.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from typing import Any, Mapping


CONTRACT_VERSION = "paired_direct_v1"
STRATEGY_FAMILY = "okx_short_perp_long_spot_basis"
ACCOUNTING_CONVENTION = "direct_reference_returns_minus_declared_costs_v1"
PAIRED_DIRECTIONS = frozenset({"short_perp_long_spot", "long_perp_short_spot"})
DEFAULT_MAX_ENTRY_TIMESTAMP_SKEW_SECONDS = 2.0
DEFAULT_MAX_EXIT_TIMESTAMP_SKEW_SECONDS = 1.0
DEFAULT_NOTIONAL_TOLERANCE_FRACTION = 0.01
DECLARED_GROSS_NOTIONAL_USD = 100.0


def is_paired_direction(value: object) -> bool:
    return str(value or "").strip().lower() in PAIRED_DIRECTIONS


def _parse_utc(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _finite(value: object, *, positive: bool = False, nonnegative: bool = False) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if positive and numeric <= 0.0:
        return None
    if nonnegative and numeric < 0.0:
        return None
    return numeric


def _limits(settings: Mapping[str, Any] | None) -> tuple[float, float, float]:
    cfg = (settings or {}).get("paper_due_outcome_collection") or {}
    entry_skew = _finite(
        cfg.get(
            "paired_max_entry_timestamp_skew_seconds",
            DEFAULT_MAX_ENTRY_TIMESTAMP_SKEW_SECONDS,
        ),
        nonnegative=True,
    )
    exit_skew = _finite(
        cfg.get(
            "paired_max_exit_timestamp_skew_seconds",
            DEFAULT_MAX_EXIT_TIMESTAMP_SKEW_SECONDS,
        ),
        nonnegative=True,
    )
    notional_tolerance = _finite(
        cfg.get(
            "paired_notional_tolerance_fraction",
            DEFAULT_NOTIONAL_TOLERANCE_FRACTION,
        ),
        nonnegative=True,
    )
    return (
        min(
            entry_skew if entry_skew is not None else DEFAULT_MAX_ENTRY_TIMESTAMP_SKEW_SECONDS,
            DEFAULT_MAX_ENTRY_TIMESTAMP_SKEW_SECONDS,
        ),
        min(
            exit_skew if exit_skew is not None else DEFAULT_MAX_EXIT_TIMESTAMP_SKEW_SECONDS,
            DEFAULT_MAX_EXIT_TIMESTAMP_SKEW_SECONDS,
        ),
        min(
            notional_tolerance
            if notional_tolerance is not None
            else DEFAULT_NOTIONAL_TOLERANCE_FRACTION,
            DEFAULT_NOTIONAL_TOLERANCE_FRACTION,
        ),
    )


def _okx_assets(inst_id: object, *, surface: str) -> tuple[str, str] | None:
    parts = str(inst_id or "").strip().upper().split("-")
    if surface == "perp":
        if len(parts) != 3 or parts[2] != "SWAP":
            return None
    elif surface == "spot":
        if len(parts) != 2:
            return None
    else:
        return None
    if not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _source_reasons(component: Mapping[str, Any], prefix: str) -> list[str]:
    source = component.get("source")
    if not isinstance(source, Mapping):
        return [f"{prefix}.source_missing"]
    return [
        f"{prefix}.source.{key}_missing"
        for key in ("name", "endpoint", "parser", "event_id")
        if not str(source.get(key) or "").strip()
    ]


def validate_paired_direct_entry(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate the exact two-leg entry contract carried by a candidate.

    ``now`` is accepted for a stable scanner/execution interface but entry
    freshness remains the queue's responsibility; this validator checks only
    immutable identity, simultaneity, notional, cost, and provenance fields.
    """

    del now
    raw = candidate.get(CONTRACT_VERSION)
    contract = copy.deepcopy(raw) if isinstance(raw, Mapping) else {}
    reasons: list[str] = []
    if not contract:
        reasons.append("paired_direct_v1_missing")
        return {"valid": False, "reasons": reasons, "contract": contract}
    if str(contract.get("contract_version") or "") != CONTRACT_VERSION:
        reasons.append("contract_version")
    if str(contract.get("strategy_family") or "") != STRATEGY_FAMILY:
        reasons.append("strategy_family")
    if str(contract.get("status") or "") != "entry_complete":
        reasons.append("entry_status")
    if str(contract.get("accounting_convention") or "") != ACCOUNTING_CONVENTION:
        reasons.append("accounting_convention")
    if str(candidate.get("direction") or "").strip().lower() != "short_perp_long_spot":
        reasons.append("direction")

    entry_skew_limit, _exit_skew_limit, notional_tolerance = _limits(settings)
    declared_entry_skew = _finite(
        contract.get("max_entry_timestamp_skew_seconds"), nonnegative=True
    )
    declared_tolerance = _finite(
        contract.get("notional_match_tolerance_fraction"), nonnegative=True
    )
    if declared_entry_skew is None or not math.isclose(
        declared_entry_skew, entry_skew_limit, rel_tol=0.0, abs_tol=1e-9
    ):
        reasons.append("max_entry_timestamp_skew_seconds")
    if declared_tolerance is None or not math.isclose(
        declared_tolerance, notional_tolerance, rel_tol=0.0, abs_tol=1e-12
    ):
        reasons.append("notional_match_tolerance_fraction")

    components = contract.get("entry_components")
    components = components if isinstance(components, Mapping) else {}
    perp = components.get("perp")
    spot = components.get("spot")
    perp = perp if isinstance(perp, Mapping) else {}
    spot = spot if isinstance(spot, Mapping) else {}
    if not perp:
        reasons.append("entry_components.perp_missing")
    if not spot:
        reasons.append("entry_components.spot_missing")
    expected = {
        "perp": (perp, "short", "OKX", "perp"),
        "spot": (spot, "long", "OKX_SPOT", "spot"),
    }
    parsed_assets: dict[str, tuple[str, str] | None] = {}
    event_times: dict[str, dt.datetime | None] = {}
    notionals: dict[str, float | None] = {}
    for name, (component, side, venue, surface) in expected.items():
        prefix = f"entry_components.{name}"
        if str(component.get("side") or "").strip().lower() != side:
            reasons.append(f"{prefix}.side")
        if str(component.get("venue") or "").strip().upper() != venue:
            reasons.append(f"{prefix}.venue")
        if str(component.get("market_surface") or "").strip().lower() != surface:
            reasons.append(f"{prefix}.market_surface")
        parsed_assets[name] = _okx_assets(component.get("inst_id"), surface=surface)
        if parsed_assets[name] is None:
            reasons.append(f"{prefix}.inst_id")
        event_times[name] = _parse_utc(component.get("event_at"))
        if event_times[name] is None:
            reasons.append(f"{prefix}.event_at")
        if _finite(component.get("price"), positive=True) is None:
            reasons.append(f"{prefix}.price")
        notionals[name] = _finite(component.get("notional_usd"), positive=True)
        if notionals[name] is None:
            reasons.append(f"{prefix}.notional_usd")
        for cost_key in (
            "entry_fee_bps",
            "entry_slippage_bps",
            "exit_fee_bps",
            "exit_slippage_bps",
        ):
            if _finite(component.get(cost_key), nonnegative=True) is None:
                reasons.append(f"{prefix}.{cost_key}")
        reasons.extend(_source_reasons(component, prefix))

    top_quote = str(contract.get("quote_asset") or "").strip().upper()
    if not top_quote:
        reasons.append("quote_asset")
    if parsed_assets.get("perp") and parsed_assets.get("spot"):
        perp_base, perp_quote = parsed_assets["perp"]  # type: ignore[misc]
        spot_base, spot_quote = parsed_assets["spot"]  # type: ignore[misc]
        if perp_base != spot_base:
            reasons.append("base_asset_mismatch")
        if perp_quote != spot_quote or perp_quote != top_quote:
            reasons.append("quote_asset_mismatch")
        for name, component in (("perp", perp), ("spot", spot)):
            if str(component.get("quote_asset") or "").strip().upper() != top_quote:
                reasons.append(f"entry_components.{name}.quote_asset")
    if event_times.get("perp") and event_times.get("spot"):
        skew = abs((event_times["perp"] - event_times["spot"]).total_seconds())  # type: ignore[operator]
        if skew > entry_skew_limit:
            reasons.append("entry_timestamp_skew")
    if notionals.get("perp") and notionals.get("spot"):
        perp_notional = float(notionals["perp"])
        spot_notional = float(notionals["spot"])
        mismatch = abs(perp_notional - spot_notional) / max(perp_notional, spot_notional)
        if mismatch > notional_tolerance:
            reasons.append("notional_mismatch")
        gross = perp_notional + spot_notional
        declared_gross = _finite(contract.get("declared_gross_notional_usd"), positive=True)
        denominator = _finite(contract.get("return_denominator_usd"), positive=True)
        if (
            declared_gross is None
            or not math.isclose(declared_gross, gross, rel_tol=1e-9, abs_tol=1e-6)
            or not math.isclose(
                declared_gross,
                DECLARED_GROSS_NOTIONAL_USD,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            reasons.append("declared_gross_notional_usd")
        if denominator is None or declared_gross is None or not math.isclose(
            denominator, declared_gross, rel_tol=1e-9, abs_tol=1e-6
        ):
            reasons.append("return_denominator_usd")

    funding = contract.get("funding_requirement")
    funding = funding if isinstance(funding, Mapping) else {}
    if not bool(funding.get("required", False)):
        reasons.append("funding_requirement.required")
    if str(funding.get("venue") or "").strip().upper() != "OKX":
        reasons.append("funding_requirement.venue")
    if str(funding.get("inst_id") or "") != str(perp.get("inst_id") or ""):
        reasons.append("funding_requirement.inst_id")
    if str(funding.get("source_endpoint") or "") != "/api/v5/public/funding-rate-history":
        reasons.append("funding_requirement.source_endpoint")
    if str(funding.get("source_parser") or "") != "okx_realized_funding_history":
        reasons.append("funding_requirement.source_parser")
    if funding.get("allow_estimates") is not False:
        reasons.append("funding_requirement.allow_estimates")

    unique_reasons = sorted(set(reasons))
    return {
        "valid": not unique_reasons,
        "reasons": unique_reasons,
        "contract": contract,
        "contract_version": CONTRACT_VERSION,
        "strategy_family": STRATEGY_FAMILY,
    }


def validate_paired_funding_coverage(
    entry_contract: Mapping[str, Any],
    coverage: Mapping[str, Any],
    target_at: object,
) -> dict[str, Any]:
    """Require authoritative realized-rate coverage across the whole holding interval."""

    reasons: list[str] = []
    components = entry_contract.get("entry_components") or {}
    perp = components.get("perp") if isinstance(components, Mapping) else {}
    perp = perp if isinstance(perp, Mapping) else {}
    entry_at = _parse_utc(perp.get("event_at"))
    target = _parse_utc(target_at)
    if entry_at is None:
        reasons.append("entry_event_at")
    if target is None or (entry_at is not None and target <= entry_at):
        reasons.append("target_at")
    if str(coverage.get("coverage_status") or "") != "complete":
        reasons.append("coverage_status")
    if coverage.get("allow_estimates") is not False:
        reasons.append("allow_estimates")
    complete_from = _parse_utc(coverage.get("complete_from"))
    complete_through = _parse_utc(coverage.get("complete_through"))
    if entry_at is not None and (complete_from is None or complete_from > entry_at):
        reasons.append("complete_from")
    if target is not None and (complete_through is None or complete_through < target):
        reasons.append("complete_through")
    query = coverage.get("query")
    query = query if isinstance(query, Mapping) else {}
    requested_from = _parse_utc(query.get("requested_from"))
    requested_through = _parse_utc(query.get("requested_through"))
    received_at = _parse_utc(query.get("received_at"))
    if query.get("request_succeeded") is not True:
        reasons.append("query.request_succeeded")
    http_status = _finite(query.get("http_status"), nonnegative=True)
    if http_status != 200.0:
        reasons.append("query.http_status")
    if query.get("pagination_complete") is not True:
        reasons.append("query.pagination_complete")
    if query.get("range_complete") is not True:
        reasons.append("query.range_complete")
    page_count = _finite(query.get("page_count"), nonnegative=True)
    if page_count is None or page_count < 1 or not float(page_count).is_integer():
        reasons.append("query.page_count")
    if not str(query.get("query_id") or "").strip():
        reasons.append("query.query_id")
    request_url = str(query.get("request_url") or "").strip()
    if not request_url.startswith(
        "https://www.okx.com/api/v5/public/funding-rate-history?"
    ):
        reasons.append("query.request_url")
    payload_sha256 = str(query.get("payload_sha256") or "").strip().lower()
    if len(payload_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in payload_sha256
    ):
        reasons.append("query.payload_sha256")
    if entry_at is not None and (requested_from is None or requested_from > entry_at):
        reasons.append("query.requested_from")
    if target is not None and (
        requested_through is None or requested_through < target
    ):
        reasons.append("query.requested_through")
    if target is not None and (received_at is None or received_at < target):
        reasons.append("query.received_at")
    source = coverage.get("source")
    source = source if isinstance(source, Mapping) else {}
    if str(source.get("endpoint") or "") != "/api/v5/public/funding-rate-history":
        reasons.append("source.endpoint")
    if str(source.get("parser") or "") != "okx_realized_funding_history":
        reasons.append("source.parser")
    if not str(source.get("name") or "").strip():
        reasons.append("source.name")
    if str(source.get("inst_id") or "") != str(perp.get("inst_id") or ""):
        reasons.append("source.inst_id")

    events = coverage.get("events")
    if not isinstance(events, list):
        reasons.append("events")
        events = []
    seen_event_ids: set[str] = set()
    normalized_events: list[dict[str, Any]] = []
    for index, raw in enumerate(events):
        event = raw if isinstance(raw, Mapping) else {}
        prefix = f"events[{index}]"
        event_at = _parse_utc(event.get("event_at"))
        realized_rate = _finite(event.get("realized_rate"))
        event_id = str(event.get("source_event_id") or "").strip()
        if event_at is None or entry_at is None or target is None or not (
            entry_at < event_at <= target
        ):
            reasons.append(f"{prefix}.event_at")
        if realized_rate is None:
            reasons.append(f"{prefix}.realized_rate")
        if not event_id or event_id in seen_event_ids:
            reasons.append(f"{prefix}.source_event_id")
        seen_event_ids.add(event_id)
        if not str(event.get("method") or "").strip():
            reasons.append(f"{prefix}.method")
        if not str(event.get("formula_type") or "").strip():
            reasons.append(f"{prefix}.formula_type")
        if event.get("estimated") is not False:
            reasons.append(f"{prefix}.estimated")
        if event_at is not None and realized_rate is not None and event_id:
            normalized_events.append(
                {
                    "event_at": event_at.isoformat(),
                    "realized_rate": realized_rate,
                    "source_event_id": event_id,
                    "method": str(event.get("method")),
                    "formula_type": str(event.get("formula_type")),
                }
            )
    normalized_events.sort(key=lambda row: row["event_at"])
    unique_reasons = sorted(set(reasons))
    return {
        "valid": not unique_reasons,
        "reasons": unique_reasons,
        "events": normalized_events,
        "coverage": copy.deepcopy(dict(coverage)),
    }


def validate_paired_direct_outcome_provenance(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the durable proof attached to a paired direct paper label.

    This is intentionally narrower than recalculating an outcome.  The raw
    observations and funding batch are immutable journal rows; consumers need
    a compact, fail-closed way to prove that the stored label names both legs,
    names its realized-funding batch, and reconciles exactly to the shared
    accounting result.
    """

    entry_validation = validate_paired_direct_entry(candidate, settings=settings)
    reasons = [f"entry.{reason}" for reason in entry_validation.get("reasons", [])]
    outcome = context.get("paired_direct_v1_outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    if str(context.get("paper_outcome_measurement_contract") or "") != CONTRACT_VERSION:
        reasons.append("paper_outcome_measurement_contract")
    if not outcome:
        reasons.append("paired_direct_v1_outcome")
    if str(outcome.get("contract_version") or "") != CONTRACT_VERSION:
        reasons.append("outcome.contract_version")
    if str(outcome.get("strategy_family") or "") != STRATEGY_FAMILY:
        reasons.append("outcome.strategy_family")
    if str(outcome.get("accounting_convention") or "") != ACCOUNTING_CONVENTION:
        reasons.append("outcome.accounting_convention")

    entry_contract = entry_validation.get("contract")
    entry_contract = entry_contract if isinstance(entry_contract, Mapping) else {}
    entry_components = entry_contract.get("entry_components")
    entry_components = entry_components if isinstance(entry_components, Mapping) else {}
    exit_components = outcome.get("exit_components")
    exit_components = exit_components if isinstance(exit_components, Mapping) else {}
    observation_ids: list[str] = []
    for component_name in ("perp", "spot"):
        exit_component = exit_components.get(component_name)
        exit_component = exit_component if isinstance(exit_component, Mapping) else {}
        entry_component = entry_components.get(component_name)
        entry_component = entry_component if isinstance(entry_component, Mapping) else {}
        prefix = f"outcome.exit_components.{component_name}"
        observation_id = str(exit_component.get("observation_id") or "").strip()
        if not observation_id:
            reasons.append(f"{prefix}.observation_id")
        else:
            observation_ids.append(observation_id)
        if str(exit_component.get("venue") or "").strip().upper() != str(
            entry_component.get("venue") or ""
        ).strip().upper():
            reasons.append(f"{prefix}.venue")
        if str(exit_component.get("inst_id") or "").strip().upper() != str(
            entry_component.get("inst_id") or ""
        ).strip().upper():
            reasons.append(f"{prefix}.inst_id")
        if _parse_utc(exit_component.get("event_at")) is None:
            reasons.append(f"{prefix}.event_at")
        if _finite(exit_component.get("price"), positive=True) is None:
            reasons.append(f"{prefix}.price")
        for source_key in ("source_parser", "source_endpoint", "source_event_id"):
            if not str(exit_component.get(source_key) or "").strip():
                reasons.append(f"{prefix}.{source_key}")
    if len(observation_ids) != 2 or len(set(observation_ids)) != 2:
        reasons.append("outcome.two_distinct_observation_ids")

    persisted_observations = context.get("paper_price_observations")
    persisted_observations = (
        persisted_observations if isinstance(persisted_observations, Mapping) else {}
    )
    for component_name in ("perp", "spot"):
        persisted = persisted_observations.get(component_name)
        persisted = persisted if isinstance(persisted, Mapping) else {}
        expected = exit_components.get(component_name)
        expected = expected if isinstance(expected, Mapping) else {}
        if str(persisted.get("observation_id") or "").strip() != str(
            expected.get("observation_id") or ""
        ).strip():
            reasons.append(f"paper_price_observations.{component_name}.observation_id")

    observed_at = _parse_utc(outcome.get("observed_at"))
    if observed_at is None:
        reasons.append("outcome.observed_at")
    funding_coverage = outcome.get("funding_coverage")
    funding_coverage = funding_coverage if isinstance(funding_coverage, Mapping) else {}
    if not str(funding_coverage.get("batch_id") or "").strip():
        reasons.append("outcome.funding_coverage.batch_id")
    funding_validation = validate_paired_funding_coverage(
        entry_contract,
        funding_coverage,
        observed_at,
    )
    reasons.extend(
        f"outcome.funding_coverage.{reason}"
        for reason in funding_validation.get("reasons", [])
    )

    accounting = outcome.get("accounting")
    accounting = accounting if isinstance(accounting, Mapping) else {}
    pnl_bps = _finite(accounting.get("pnl_bps"))
    reconciliation_sum = _finite(accounting.get("reconciliation_sum_bps"))
    reconciliation_error = _finite(accounting.get("reconciliation_error_bps"))
    if pnl_bps is None:
        reasons.append("outcome.accounting.pnl_bps")
    if reconciliation_sum is None:
        reasons.append("outcome.accounting.reconciliation_sum_bps")
    if reconciliation_error is None or not math.isclose(
        reconciliation_error, 0.0, rel_tol=0.0, abs_tol=1e-9
    ):
        reasons.append("outcome.accounting.reconciliation_error_bps")
    if (
        pnl_bps is not None
        and reconciliation_sum is not None
        and not math.isclose(pnl_bps, reconciliation_sum, rel_tol=0.0, abs_tol=1e-9)
    ):
        reasons.append("outcome.accounting.reconciliation_sum_mismatch")

    unique_reasons = sorted(set(reasons))
    return {
        "valid": not unique_reasons,
        "reasons": unique_reasons,
        "entry_validation": entry_validation,
        "outcome": copy.deepcopy(dict(outcome)),
        "observation_ids": observation_ids,
        "funding_batch_id": str(funding_coverage.get("batch_id") or "") or None,
    }


def _exit_component_reasons(
    name: str,
    observation: Mapping[str, Any],
    entry_component: Mapping[str, Any],
    target: dt.datetime,
    max_delay_seconds: float,
) -> tuple[list[str], dt.datetime | None, float | None]:
    prefix = f"exit_components.{name}"
    reasons: list[str] = []
    if str(observation.get("source_kind") or "") != "exchange_candle_1m_close":
        reasons.append(f"{prefix}.source_kind")
    if not bool(observation.get("is_closed", False)) or bool(
        observation.get("is_partial", True)
    ):
        reasons.append(f"{prefix}.closed_candle")
    if str(observation.get("venue") or "").upper() != str(
        entry_component.get("venue") or ""
    ).upper():
        reasons.append(f"{prefix}.venue")
    if str(observation.get("inst_id") or "") != str(entry_component.get("inst_id") or ""):
        reasons.append(f"{prefix}.inst_id")
    event_at = _parse_utc(observation.get("event_at"))
    if event_at is None or event_at < target or (
        event_at - target
    ).total_seconds() > max_delay_seconds:
        reasons.append(f"{prefix}.event_at")
    price = _finite(observation.get("price"), positive=True)
    if price is None:
        reasons.append(f"{prefix}.price")
    if not str(observation.get("observation_id") or "").strip():
        reasons.append(f"{prefix}.observation_id")
    for key in ("source_parser", "source_endpoint", "source_event_id"):
        if not str(observation.get(key) or "").strip():
            reasons.append(f"{prefix}.{key}")
    return reasons, event_at, price


def calculate_paired_direct_outcome(
    candidate: Mapping[str, Any],
    exit_components: Mapping[str, Any],
    funding_coverage: Mapping[str, Any],
    target_at: object,
    *,
    max_delay_seconds: float = 300.0,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate both exits/funding and calculate one composite paired return."""

    entry_validation = validate_paired_direct_entry(candidate, settings=settings)
    reasons = list(entry_validation["reasons"])
    contract = entry_validation["contract"]
    target = _parse_utc(target_at)
    if target is None:
        reasons.append("target_at")
    delay_limit = _finite(max_delay_seconds, nonnegative=True)
    if delay_limit is None:
        reasons.append("max_delay_seconds")
        delay_limit = 0.0
    components = contract.get("entry_components") or {}
    exit_times: dict[str, dt.datetime | None] = {}
    exit_prices: dict[str, float | None] = {}
    if target is not None and isinstance(components, Mapping):
        for name in ("perp", "spot"):
            observation = exit_components.get(name)
            observation = observation if isinstance(observation, Mapping) else {}
            entry_component = components.get(name)
            entry_component = entry_component if isinstance(entry_component, Mapping) else {}
            component_reasons, event_at, price = _exit_component_reasons(
                name,
                observation,
                entry_component,
                target,
                delay_limit,
            )
            reasons.extend(component_reasons)
            exit_times[name] = event_at
            exit_prices[name] = price
    else:
        reasons.append("entry_components")
    _entry_limit, exit_skew_limit, _notional_tolerance = _limits(settings)
    if exit_times.get("perp") and exit_times.get("spot"):
        exit_skew = abs((exit_times["perp"] - exit_times["spot"]).total_seconds())  # type: ignore[operator]
        if exit_skew > exit_skew_limit:
            reasons.append("exit_timestamp_skew")

    coverage_through = (
        max(exit_times["perp"], exit_times["spot"])
        if exit_times.get("perp") and exit_times.get("spot")
        else target
    )
    funding_validation = validate_paired_funding_coverage(
        contract,
        funding_coverage,
        coverage_through,
    )
    reasons.extend(funding_validation["reasons"])
    unique_reasons = sorted(set(reasons))
    if unique_reasons:
        return {
            "valid": False,
            "reasons": unique_reasons,
            "contract_version": CONTRACT_VERSION,
            "strategy_family": STRATEGY_FAMILY,
        }

    perp = components["perp"]
    spot = components["spot"]
    perp_notional = float(perp["notional_usd"])
    spot_notional = float(spot["notional_usd"])
    perp_entry = float(perp["price"])
    spot_entry = float(spot["price"])
    perp_exit = float(exit_prices["perp"])
    spot_exit = float(exit_prices["spot"])
    perp_gross_pnl_usd = perp_notional * (perp_entry - perp_exit) / perp_entry
    spot_gross_pnl_usd = spot_notional * (spot_exit - spot_entry) / spot_entry
    entry_cost_usd = sum(
        float(component["notional_usd"])
        * (float(component["entry_fee_bps"]) + float(component["entry_slippage_bps"]))
        / 10_000.0
        for component in (perp, spot)
    )
    exit_cost_usd = sum(
        float(component["notional_usd"])
        * (float(component["exit_fee_bps"]) + float(component["exit_slippage_bps"]))
        / 10_000.0
        for component in (perp, spot)
    )
    realized_funding_usd = sum(
        perp_notional * float(event["realized_rate"])
        for event in funding_validation["events"]
    )
    net_pnl_usd = (
        perp_gross_pnl_usd
        + spot_gross_pnl_usd
        + realized_funding_usd
        - entry_cost_usd
        - exit_cost_usd
    )
    denominator = float(contract["return_denominator_usd"])
    pnl_bps = net_pnl_usd / denominator * 10_000.0
    reconciliation_bps = {
        "perp_price_pnl_bps": perp_gross_pnl_usd / denominator * 10_000.0,
        "spot_price_pnl_bps": spot_gross_pnl_usd / denominator * 10_000.0,
        "realized_funding_bps": realized_funding_usd / denominator * 10_000.0,
        "entry_cost_bps": -entry_cost_usd / denominator * 10_000.0,
        "exit_cost_bps": -exit_cost_usd / denominator * 10_000.0,
    }
    reconciliation_sum_bps = sum(reconciliation_bps.values())
    observed_at = max(exit_times["perp"], exit_times["spot"])  # type: ignore[type-var]
    delay_seconds = max(0.0, (observed_at - target).total_seconds())  # type: ignore[operator]
    provenance = {
        "contract_version": CONTRACT_VERSION,
        "strategy_family": STRATEGY_FAMILY,
        "accounting_convention": ACCOUNTING_CONVENTION,
        "target_at": target.isoformat() if target else None,
        "observed_at": observed_at.isoformat(),
        "exit_components": {
            name: {
                key: exit_components[name].get(key)
                for key in (
                    "observation_id",
                    "venue",
                    "inst_id",
                    "event_at",
                    "price",
                    "source_parser",
                    "source_endpoint",
                    "source_event_id",
                )
            }
            for name in ("perp", "spot")
        },
        "funding_coverage": funding_validation["coverage"],
        "accounting": {
            "perp_gross_pnl_usd": perp_gross_pnl_usd,
            "spot_gross_pnl_usd": spot_gross_pnl_usd,
            "realized_funding_usd": realized_funding_usd,
            "entry_cost_usd": entry_cost_usd,
            "exit_cost_usd": exit_cost_usd,
            "net_pnl_usd": net_pnl_usd,
            "return_denominator_usd": denominator,
            "pnl_bps": pnl_bps,
            "reconciliation_bps": reconciliation_bps,
            "reconciliation_sum_bps": reconciliation_sum_bps,
            "reconciliation_error_bps": pnl_bps - reconciliation_sum_bps,
        },
    }
    return {
        "valid": True,
        "reasons": [],
        "contract_version": CONTRACT_VERSION,
        "strategy_family": STRATEGY_FAMILY,
        "pnl_bps": pnl_bps,
        "net_pnl_usd": net_pnl_usd,
        "price": perp_exit,
        "observed_at": observed_at.isoformat(),
        "delay_seconds": delay_seconds,
        "price_source": CONTRACT_VERSION,
        "context": provenance,
    }
