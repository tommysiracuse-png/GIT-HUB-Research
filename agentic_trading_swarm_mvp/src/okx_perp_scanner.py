#!/usr/bin/env python3
"""OKX perpetual swap opportunity scanner.

This is a fast, dependency-free market sensor for the agent swarm MVP.
It writes ranked short-term dislocation candidates to JSON.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import collections
import datetime as dt
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from scan_batch import ScanBatch
from paper_context_cost import annotate_paper_context_cost
from paired_direct_contract import (
    ACCOUNTING_CONVENTION as PAIRED_DIRECT_ACCOUNTING_CONVENTION,
    CONTRACT_VERSION as PAIRED_DIRECT_CONTRACT_VERSION,
    DECLARED_GROSS_NOTIONAL_USD as PAIRED_DIRECT_GROSS_NOTIONAL_USD,
    STRATEGY_FAMILY as PAIRED_DIRECT_STRATEGY_FAMILY,
    validate_paired_direct_entry,
)


BASE_URL = "https://www.okx.com"
ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
STRATEGY_OBSERVATION_FIELDS = {
    "basis_bps",
    "mark_basis_bps",
    "basis_mark_last_delta_bps",
    "funding_bps",
    "funding_interval_hours",
    "next_funding_time",
    "time_to_next_funding_minutes",
    "funding_history_count",
    "funding_history_avg_bps",
    "funding_history_last_bps",
    "funding_history_min_bps",
    "funding_history_max_bps",
    "funding_history_slope_bps",
    "spread_bps",
    "liquidity_score",
    "quality_score",
    "quality_status",
    "quote_volume_24h",
    "change_24h_pct",
}
PAIRED_DIRECT_EXCLUSION_REASON = "paired_direct_contract_invalid_or_incomplete"
OKX_TICKER_SOURCE_NAME = "OKX public REST market tickers"
OKX_TICKER_SOURCE_PARSER = "okx_v5_market_tickers"


def fetch_json(path: str, params: dict[str, str] | None = None, timeout: int = 12) -> dict:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        BASE_URL + path + query,
        headers={"User-Agent": "inefficiency-radar/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error for {path}: {data}")
    return data


def as_float(value: str | None, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def bps(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator - 1.0) * 10_000.0


def unix_ms_to_iso(value: str | int | None) -> str | None:
    if not value:
        return None
    try:
        stamp = int(value) / 1000.0
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    try:
        return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _ticker_event_provenance(row: dict, received_at: str) -> dict:
    """Keep exchange event time distinct from the local scan receipt time."""

    event_at = unix_ms_to_iso(row.get("ts"))
    timestamp_ms = as_float(row.get("ts"), None)
    provenance = {"received_at": received_at}
    if (
        event_at is None
        or timestamp_ms is None
        or not math.isfinite(timestamp_ms)
        or timestamp_ms <= 0.0
    ):
        return provenance
    provenance.update(
        {
            "ticker_timestamp_ms": int(timestamp_ms),
            "exchange_timestamp": event_at,
            "ticker_timestamp": event_at,
            "source_observed_at": event_at,
            "observed_at": event_at,
        }
    )
    return provenance


def liquidity_score(quote_volume: float) -> float:
    if quote_volume <= 0:
        return 0.0
    # Smoothly maps roughly $100k -> low, $10m+ -> high.
    return max(0.0, min(1.0, (math.log10(quote_volume) - 5.0) / 3.0))


def classify_direction(funding_bps: float, basis_bps: float) -> tuple[str, str]:
    if funding_bps > 0 and basis_bps > 0:
        return "short_perp_long_spot", "positive funding plus positive perp/index basis"
    if funding_bps < 0 and basis_bps < 0:
        return "long_perp_short_spot", "negative funding plus negative perp/index basis"
    if abs(basis_bps) > 25:
        direction = "basis_mean_reversion_short_perp" if basis_bps > 0 else "basis_mean_reversion_long_perp"
        return direction, "large perp/index basis without funding confirmation"
    if abs(funding_bps) > 3:
        direction = "funding_capture_short_perp" if funding_bps > 0 else "funding_capture_long_perp"
        return direction, "large funding rate without basis confirmation"
    return "watch_only", "weak or mixed signal"


def execution_feasibility(direction: str, allow_short_spot: bool) -> dict:
    """Describe whether the trade can be executed without hard-to-source legs."""
    if direction == "short_perp_long_spot":
        return {
            "status": "standard",
            "requires_short_spot": False,
            "legs": ["short perpetual", "long spot"],
            "notes": [
                "Both legs are mandatory; an index reference can never replace the spot fill.",
                "Direct, fresh, timestamp-aligned SWAP and SPOT quotes are required before paper execution.",
            ],
        }
    if direction in {"funding_capture_short_perp", "basis_mean_reversion_short_perp"}:
        return {
            "status": "standard",
            "requires_short_spot": False,
            "legs": ["short perpetual"],
            "notes": [
                "This is an unhedged single-perpetual paper strategy, not a paired basis strategy."
            ],
        }
    if direction == "long_perp_short_spot":
        status = "standard" if allow_short_spot else "conditional"
        return {
            "status": status,
            "requires_short_spot": True,
            "legs": ["long perpetual", "borrow and short spot"],
            "notes": [
                "Reverse cash-and-carry requires confirmed spot borrow or a margin venue.",
                "Without spot borrow, this becomes a directional long-perp trade, not a hedged arb.",
            ],
        }
    if direction in {"funding_capture_long_perp", "basis_mean_reversion_long_perp"}:
        return {
            "status": "standard",
            "requires_short_spot": False,
            "legs": ["long perpetual"],
            "notes": [
                "This is an unhedged single-perpetual paper strategy, not a paired basis strategy."
            ],
        }
    return {
        "status": "watch_only",
        "requires_short_spot": False,
        "legs": [],
        "notes": ["Signal is not strong or clean enough for execution review."],
    }


def score_candidate(row: dict) -> float:
    funding_signal = min(abs(row["funding_bps"]) * 4.0, 30.0)
    basis_signal = min(abs(row["basis_bps"]) * 0.7, 35.0)
    momentum_context = min(abs(row["change_24h_pct"]) * 0.6, 12.0)
    liquidity = row["liquidity_score"] * 20.0
    spread_penalty = min(row["spread_bps"] * 2.5, 25.0)
    mixed_penalty = 8.0 if row["direction"] == "watch_only" else 0.0
    feasibility_penalty = 12.0 if row.get("execution_feasibility", {}).get("status") == "conditional" else 0.0
    score = (
        funding_signal
        + basis_signal
        + momentum_context
        + liquidity
        - spread_penalty
        - mixed_penalty
        - feasibility_penalty
    )
    return round(max(0.0, score), 2)


def instrument_asset_context(inst_id: str, instrument: dict | None = None) -> dict:
    instrument = instrument or {}
    normalized_inst_id = str(inst_id or "").split(":")[-1]
    parts = [part for part in normalized_inst_id.upper().split("-") if part]
    base_asset = str(instrument.get("baseCcy") or (parts[0] if parts else "")).upper()
    quote_asset = str(
        instrument.get("quoteCcy")
        or instrument.get("settleCcy")
        or (parts[1] if len(parts) > 1 else "")
    ).upper()
    instrument_family = str(
        instrument.get("instFamily")
        or instrument.get("uly")
        or (f"{base_asset}-{quote_asset}" if base_asset and quote_asset else "")
    ).upper()
    index_id = f"{base_asset}-{quote_asset}" if base_asset and quote_asset else normalized_inst_id.replace("-SWAP", "")
    return {
        "base": base_asset or None,
        "quote": quote_asset or None,
        "base_asset": base_asset or None,
        "quote_asset": quote_asset or None,
        "settlement_currency": str(instrument.get("settleCcy") or quote_asset or "").upper() or None,
        "underlying": str(instrument.get("uly") or instrument_family or index_id).upper() or None,
        "instrument_type": "perpetual_swap" if normalized_inst_id.endswith("-SWAP") else "derivative",
        "asset_class": "crypto_linked_derivative",
        "market_surface": "okx_perpetual_swap",
        "instrument_family": instrument_family or None,
        "index_id": index_id,
        "basis_context_key": f"OKX|{instrument_family or index_id}",
        "basis_context_status": "asset_specific" if base_asset and quote_asset and instrument_family else "unresolved",
    }


def _parse_utc(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _paired_direct_limits(settings: dict | None) -> tuple[float, float, float]:
    settings = settings or {}
    outcome_cfg = settings.get("paper_due_outcome_collection") or {}
    queue_cfg = (settings.get("market_admission") or {}).get("paper_queue") or {}
    try:
        max_skew = max(
            0.0,
            float(outcome_cfg.get("paired_max_entry_timestamp_skew_seconds", 2.0)),
        )
    except (TypeError, ValueError):
        max_skew = 2.0
    try:
        notional_tolerance = max(
            0.0,
            float(outcome_cfg.get("paired_notional_tolerance_fraction", 0.01)),
        )
    except (TypeError, ValueError):
        notional_tolerance = 0.01
    try:
        max_age = max(
            1.0,
            float(queue_cfg.get("max_freshness_age_seconds", 90.0)),
        )
    except (TypeError, ValueError):
        max_age = 90.0
    return max_skew, notional_tolerance, max_age


def _paired_direct_costs(settings: dict | None) -> tuple[float, float]:
    risk = (settings or {}).get("risk") or {}
    try:
        fee_bps = max(0.0, float(risk.get("taker_fee_bps_per_leg", 5.0)))
    except (TypeError, ValueError):
        fee_bps = 5.0
    try:
        slippage_bps = max(0.0, float(risk.get("slippage_bps_per_leg", 3.0)))
    except (TypeError, ValueError):
        slippage_bps = 3.0
    return fee_bps, slippage_bps


def _paired_direct_component(
    *,
    role: str,
    inst_id: str,
    row: dict,
    quote_asset: str,
    notional_usd: float,
    fee_bps: float,
    slippage_bps: float,
) -> dict:
    is_perp = role == "perp"
    side = "short" if is_perp else "long"
    venue = "OKX" if is_perp else "OKX_SPOT"
    surface = "perp" if is_perp else "spot"
    executable_field = "bidPx" if is_perp else "askPx"
    price = as_float(row.get(executable_field), None)
    event_at = unix_ms_to_iso(row.get("ts"))
    endpoint = f"/api/v5/market/tickers?instType={'SWAP' if is_perp else 'SPOT'}"
    event_id = (
        f"OKX|{surface}|{inst_id}|{event_at}"
        if event_at and inst_id
        else ""
    )
    return {
        "side": side,
        "venue": venue,
        "inst_id": inst_id,
        "market_surface": surface,
        "quote_asset": quote_asset,
        "event_at": event_at,
        "price": price if price is not None and price > 0.0 else None,
        "notional_usd": notional_usd,
        "entry_fee_bps": fee_bps,
        "entry_slippage_bps": slippage_bps,
        "exit_fee_bps": fee_bps,
        "exit_slippage_bps": slippage_bps,
        "source": {
            "name": OKX_TICKER_SOURCE_NAME,
            "endpoint": endpoint,
            "parser": OKX_TICKER_SOURCE_PARSER,
            "event_id": event_id,
        },
    }


def apply_paired_direct_entry_contract(
    candidate: dict,
    perp_ticker: dict,
    spot_ticker: dict | None,
    settings: dict | None,
    *,
    decision_time: dt.datetime,
) -> dict:
    """Attach a directly quoted, two-leg OKX entry contract or fail closed."""

    output = dict(candidate)
    quote_asset = str(output.get("quote_asset") or "").strip().upper()
    base_asset = str(output.get("base_asset") or "").strip().upper()
    perp_inst_id = str(output.get("inst_id") or "").strip().upper()
    spot_inst_id = f"{base_asset}-{quote_asset}" if base_asset and quote_asset else ""
    spot_ticker = dict(spot_ticker or {})
    max_skew, notional_tolerance, max_age = _paired_direct_limits(settings)
    fee_bps, slippage_bps = _paired_direct_costs(settings)
    leg_notional = PAIRED_DIRECT_GROSS_NOTIONAL_USD / 2.0
    perp = _paired_direct_component(
        role="perp",
        inst_id=perp_inst_id,
        row=perp_ticker,
        quote_asset=quote_asset,
        notional_usd=leg_notional,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )
    spot = _paired_direct_component(
        role="spot",
        inst_id=spot_inst_id,
        row=spot_ticker,
        quote_asset=quote_asset,
        notional_usd=leg_notional,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
    )

    producer_reasons: list[str] = []
    if str(perp_ticker.get("instId") or "").strip().upper() != perp_inst_id:
        producer_reasons.append("direct_perp_ticker_identity_mismatch")
    if str(spot_ticker.get("instId") or "").strip().upper() != spot_inst_id:
        producer_reasons.append("direct_spot_ticker_missing")
    event_times = {
        "perp": _parse_utc(perp.get("event_at")),
        "spot": _parse_utc(spot.get("event_at")),
    }
    for name, component in (("perp", perp), ("spot", spot)):
        if component.get("price") is None:
            producer_reasons.append(f"{name}_executable_price_missing")
        event_at = event_times[name]
        if event_at is None:
            producer_reasons.append(f"{name}_event_at_missing")
            continue
        age_seconds = (decision_time - event_at).total_seconds()
        component["freshness_age_seconds"] = round(max(0.0, age_seconds), 3)
        if age_seconds < 0.0:
            producer_reasons.append(f"{name}_event_at_in_future")
        elif age_seconds > max_age:
            producer_reasons.append(f"{name}_quote_stale")
    if event_times["perp"] is not None and event_times["spot"] is not None:
        skew = abs((event_times["perp"] - event_times["spot"]).total_seconds())
        if skew > max_skew:
            producer_reasons.append("entry_timestamp_skew")
    else:
        skew = None

    contract = {
        "contract_version": PAIRED_DIRECT_CONTRACT_VERSION,
        "strategy_family": PAIRED_DIRECT_STRATEGY_FAMILY,
        "accounting_convention": PAIRED_DIRECT_ACCOUNTING_CONVENTION,
        "status": "entry_complete" if not producer_reasons else "invalid_or_incomplete",
        "quote_asset": quote_asset,
        "max_entry_timestamp_skew_seconds": max_skew,
        "max_entry_freshness_age_seconds": max_age,
        "entry_timestamp_skew_seconds": round(skew, 3) if skew is not None else None,
        "notional_match_tolerance_fraction": notional_tolerance,
        "declared_gross_notional_usd": PAIRED_DIRECT_GROSS_NOTIONAL_USD,
        "return_denominator_usd": PAIRED_DIRECT_GROSS_NOTIONAL_USD,
        "entry_components": {"perp": perp, "spot": spot},
        "funding_requirement": {
            "required": True,
            "venue": "OKX",
            "inst_id": perp_inst_id,
            "source_endpoint": "/api/v5/public/funding-rate-history",
            "source_parser": "okx_realized_funding_history",
            "allow_estimates": False,
            "rate_field": "realizedRate",
            "event_time_field": "fundingTime",
            "window_semantics": "entry_exclusive_exit_inclusive",
        },
        "cost_accounting": {
            "version": "reference_prices_plus_explicit_costs_v1",
            "price_basis": "direct_executable_top_of_book_reference",
            "fees_deducted_once": True,
            "slippage_deducted_once": True,
            "modeled_fill_price_audit_only": True,
        },
        "producer_validation_reasons": sorted(set(producer_reasons)),
    }
    output[PAIRED_DIRECT_CONTRACT_VERSION] = contract
    validation = validate_paired_direct_entry(
        output,
        settings=settings,
        now=decision_time,
    )
    validation_reasons = sorted(set(producer_reasons + list(validation["reasons"])))
    valid = bool(validation["valid"] and not producer_reasons)
    if not valid:
        contract["status"] = "invalid_or_incomplete"
        contract["validation_reasons"] = validation_reasons

    output.update(
        {
            "contract_version": PAIRED_DIRECT_CONTRACT_VERSION,
            "strategy_family": PAIRED_DIRECT_STRATEGY_FAMILY,
            "paired_direct_contract_status": (
                "entry_complete" if valid else "invalid_or_incomplete"
            ),
            "execution_structure": "perpetual_spot_pair",
            "hedge_venue": "OKX_SPOT",
            "hedge_instrument": spot_inst_id,
            "fee_model": "paired_direct_reference_costs_v1",
            "fees_modeled": True,
            "paper_leg_mapping_valid": valid,
            "perp_last": as_float(perp_ticker.get("last"), None),
            "spot_last": as_float(spot_ticker.get("last"), None),
            "paper_label_eligible": valid,
            "paper_label_exclusion_reason": (
                None if valid else PAIRED_DIRECT_EXCLUSION_REASON
            ),
            "paper_execution_semantics": (
                PAIRED_DIRECT_CONTRACT_VERSION
                if valid
                else "paired_direct_incomplete_shadow"
            ),
            "signal_stats_scope": "direct" if valid else "shadow",
            "paper_fill_allowed": valid,
        }
    )
    complete_event_times = [value for value in event_times.values() if value is not None]
    if complete_event_times:
        oldest_entry_event = min(complete_event_times)
        latest_entry_event = max(complete_event_times)
        # Admission freshness must be anchored to the oldest required leg, not
        # the later quote that would make a stale pair look fresh.
        output["source_observed_at"] = oldest_entry_event.isoformat()
        output["observed_at"] = latest_entry_event.isoformat()
        output["signal_age_seconds"] = round(
            max(0.0, (decision_time - latest_entry_event).total_seconds()),
            3,
        )
        output["freshness_age_seconds"] = max(
            float(component.get("freshness_age_seconds") or 0.0)
            for component in (perp, spot)
        )
        output["stale_minutes"] = round(
            float(output["freshness_age_seconds"]) / 60.0,
            3,
        )
    if valid:
        output["execution_venue"] = "OKX"
        output["venue_capabilities"] = {
            "supports_perpetuals": True,
            "supports_basis_path": True,
            "supports_basis_carry": True,
            "supports_spot_long": True,
            "supports_spot_short": False,
            "capability_profile": "OKX_PAIRED_DIRECT_V1",
        }
        output["paper_legs"] = [dict(perp), dict(spot)]
        output["execution_feasibility"] = {
            "status": "standard",
            "route_status": "standard",
            "requires_short_spot": False,
            "legs": ["short perpetual", "long spot"],
            "missing_requirements": [],
            "notes": [
                "Both OKX legs are directly quoted under paired_direct_v1.",
                "Realized funding history remains mandatory for any reliable outcome.",
            ],
        }
    else:
        output.update(
            {
                "paper_entry_blocked": True,
                "shadow_filtered": True,
                "paper_observation_only": True,
                "paper_shadow_excluded_from_learning": True,
                "paper_shadow_exclusion_reason": PAIRED_DIRECT_EXCLUSION_REASON,
                "candidate_reject_reason": PAIRED_DIRECT_EXCLUSION_REASON,
                "candidate_reject_detail": {
                    "guard": PAIRED_DIRECT_CONTRACT_VERSION,
                    "reasons": validation_reasons,
                },
            }
        )
        output["execution_feasibility"] = {
            "status": "conditional",
            "route_status": "conditional",
            "requires_short_spot": False,
            "legs": ["short perpetual", "long spot"],
            "missing_requirements": ["paired_direct_v1_entry_complete"],
            "notes": [
                "Paired paper execution is shadow-only until both direct entry quotes satisfy paired_direct_v1."
            ],
        }
    return output


def get_funding(inst_id: str) -> dict:
    data = fetch_json("/api/v5/public/funding-rate", {"instId": inst_id})
    return data["data"][0] if data.get("data") else {}


def get_funding_history(inst_id: str, limit: int = 20) -> dict:
    data = fetch_json("/api/v5/public/funding-rate-history", {"instId": inst_id, "limit": str(limit)})
    rows = data.get("data") or []
    values = [as_float(row.get("fundingRate")) * 10_000.0 for row in rows if row.get("fundingRate") not in (None, "")]
    if not values:
        return {"funding_history_count": 0}
    newest = values[0]
    oldest = values[-1]
    return {
        "funding_history_count": len(values),
        "funding_history_avg_bps": round(sum(values) / len(values), 4),
        "funding_history_min_bps": round(min(values), 4),
        "funding_history_max_bps": round(max(values), 4),
        "funding_history_last_bps": round(newest, 4),
        "funding_history_slope_bps": round(newest - oldest, 4),
    }


def _safe_public_map(path: str, params: dict[str, str], key: str, value_keys: tuple[str, ...]) -> dict[str, dict]:
    try:
        rows = fetch_json(path, params).get("data") or []
    except Exception:
        return {}
    output: dict[str, dict] = {}
    for row in rows:
        inst_id = str(row.get(key) or "")
        if not inst_id:
            continue
        # Preserve the identity used to key the response so downstream paired
        # contracts can verify the payload row instead of trusting lookup state.
        output[inst_id] = {
            key: inst_id,
            **{name: row.get(name) for name in value_keys},
        }
    return output


def _funding_interval_hours(funding: dict) -> float | None:
    current = funding.get("fundingTime")
    nxt = funding.get("nextFundingTime")
    try:
        if not current or not nxt:
            return None
        return round((int(nxt) - int(current)) / 3_600_000.0, 3)
    except (TypeError, ValueError):
        return None


def _basis_bucket(value: float) -> str:
    abs_value = abs(value)
    if abs_value < 10:
        return "small"
    if abs_value < 30:
        return "moderate"
    if abs_value < 75:
        return "large"
    return "extreme"


def _minutes_until(value: str | None, now: dt.datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return round(max(0.0, (parsed.astimezone(dt.timezone.utc) - now).total_seconds() / 60.0), 3)


def _market_data_quality(
    *,
    index_price: float,
    funding_rate: float | None,
    bid: float,
    ask: float,
    spread_bps: float,
    signal_age_seconds: float | None,
    funding_history_count: int,
) -> tuple[float, str]:
    """Score only explicit public-data completeness and freshness."""

    score = 100.0
    if index_price <= 0:
        score -= 40.0
    if funding_rate is None:
        score -= 30.0
    if bid <= 0 or ask <= bid:
        score -= 25.0
    elif spread_bps > 0:
        score -= min(20.0, spread_bps / 2.0)
    if signal_age_seconds is None:
        score -= 10.0
    elif signal_age_seconds > 120.0:
        score -= min(30.0, (signal_age_seconds - 120.0) / 30.0)
    if funding_history_count < 3:
        score -= 10.0
    bounded = round(max(0.0, min(100.0, score)), 3)
    status = "verified" if bounded >= 75.0 else "degraded" if bounded >= 60.0 else "unavailable"
    return bounded, status


def _strategy_observation(row: dict, candidate: dict | None, instrument: dict | None, seen_at: str) -> dict:
    observation = {
        "inst_id": row.get("instId"),
        "venue": "OKX",
        "trade_type": "perp_funding_basis",
        **instrument_asset_context(str(row.get("instId") or ""), instrument),
        "last": as_float(row.get("last")),
        **_ticker_event_provenance(row, seen_at),
        "price_source": "OKX public REST market tickers",
    }
    if not candidate:
        return observation
    for field in STRATEGY_OBSERVATION_FIELDS:
        if candidate.get(field) is not None:
            observation[field] = candidate[field]
    if candidate.get("signal_age_seconds") is not None:
        observation["freshness_age_seconds"] = candidate["signal_age_seconds"]
        observation["stale_minutes"] = round(float(candidate["signal_age_seconds"]) / 60.0, 3)
    return observation


def build_scan_batch(
    scan_universe: int,
    allow_short_spot: bool = False,
    required_inst_ids: set[str] | None = None,
    enrichment_limit: int = 30,
    settings: dict | None = None,
) -> ScanBatch:
    tickers = fetch_json("/api/v5/market/tickers", {"instType": "SWAP"})["data"]
    index_rows = fetch_json("/api/v5/market/index-tickers", {"quoteCcy": "USDT"})["data"]
    index_by_id = {row["instId"]: as_float(row.get("idxPx")) for row in index_rows}
    mark_by_inst = _safe_public_map(
        "/api/v5/public/mark-price",
        {"instType": "SWAP"},
        "instId",
        ("markPx", "ts"),
    )
    open_interest_by_inst = _safe_public_map(
        "/api/v5/public/open-interest",
        {"instType": "SWAP"},
        "instId",
        ("oi", "oiCcy", "oiUsd", "ts"),
    )
    instrument_by_inst = _safe_public_map(
        "/api/v5/public/instruments",
        {"instType": "SWAP"},
        "instId",
        (
            "ctVal", "ctValCcy", "settleCcy", "state", "tickSz", "lotSz", "minSz",
            "baseCcy", "quoteCcy", "instFamily", "uly", "ctType",
        ),
    )
    spot_ticker_by_inst = _safe_public_map(
        "/api/v5/market/tickers",
        {"instType": "SPOT"},
        "instId",
        ("last", "bidPx", "askPx", "volCcy24h", "ts"),
    )

    usdt_swaps = []
    for row in tickers:
        inst_id = row.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        quote_volume = as_float(row.get("volCcy24h"))
        if quote_volume <= 0:
            continue
        usdt_swaps.append((quote_volume, row))

    usdt_swaps.sort(key=lambda item: item[0], reverse=True)
    required_inst_ids = required_inst_ids or set()
    selected_by_id = {
        row["instId"]: row
        for _, row in usdt_swaps[:scan_universe]
    }
    for _, row in usdt_swaps:
        if row.get("instId") in required_inst_ids:
            selected_by_id[row["instId"]] = row
    selected = list(selected_by_id.values())

    funding_by_inst: dict[str, dict] = {}
    history_by_inst: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(get_funding, row["instId"]): row["instId"] for row in selected}
        for future in concurrent.futures.as_completed(futures):
            inst_id = futures[future]
            try:
                funding_by_inst[inst_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                funding_by_inst[inst_id] = {"error": str(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        history_targets = selected[: max(0, int(enrichment_limit))]
        futures = {pool.submit(get_funding_history, row["instId"]): row["instId"] for row in history_targets}
        for future in concurrent.futures.as_completed(futures):
            inst_id = futures[future]
            try:
                history_by_inst[inst_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                history_by_inst[inst_id] = {"funding_history_error": str(exc)[:180]}

    candidates = []
    decision_time = dt.datetime.now(dt.timezone.utc)
    seen_at = decision_time.isoformat()
    direction_counts: collections.Counter[str] = collections.Counter()
    for row in selected:
        inst_id = row["instId"]
        inst = instrument_by_inst.get(inst_id, {})
        asset_context = instrument_asset_context(inst_id, inst)
        index_id = str(asset_context["index_id"])
        idx_px = index_by_id.get(index_id, 0.0)
        last = as_float(row.get("last"))
        mark_row = mark_by_inst.get(inst_id, {})
        mark_px = as_float(mark_row.get("markPx"), last)
        bid = as_float(row.get("bidPx"))
        ask = as_float(row.get("askPx"))
        open_24h = as_float(row.get("open24h"))
        quote_volume = as_float(row.get("volCcy24h"))
        ticker_timestamp_ms = as_float(row.get("ts"), None)
        signal_age_seconds = (
            max(0.0, decision_time.timestamp() - ticker_timestamp_ms / 1000.0)
            if ticker_timestamp_ms is not None and ticker_timestamp_ms > 0.0
            else None
        )
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if ask > bid and mid > 0 else 999.0
        basis_bps = bps(last, idx_px) if idx_px > 0 else 0.0
        mark_basis_bps = bps(mark_px, idx_px) if idx_px > 0 else basis_bps
        change_24h_pct = bps(last, open_24h) / 100.0 if open_24h > 0 else 0.0

        funding = funding_by_inst.get(inst_id, {})
        funding_rate = as_float(funding.get("fundingRate"), None)
        funding_bps = funding_rate * 10_000.0 if funding_rate is not None else 0.0
        next_funding_time = unix_ms_to_iso(funding.get("nextFundingTime") or funding.get("fundingTime"))
        funding_interval_hours = _funding_interval_hours(funding)
        direction, thesis = classify_direction(funding_bps, basis_bps)
        feasibility = execution_feasibility(direction, allow_short_spot)
        oi = open_interest_by_inst.get(inst_id, {})
        history = history_by_inst.get(inst_id, {"funding_history_count": 0})
        quality_score, quality_status = _market_data_quality(
            index_price=idx_px,
            funding_rate=funding_rate,
            bid=bid,
            ask=ask,
            spread_bps=spread_bps,
            signal_age_seconds=signal_age_seconds,
            funding_history_count=int(history.get("funding_history_count") or 0),
        )
        basis_same_sign = basis_bps == 0.0 or mark_basis_bps == 0.0 or basis_bps * mark_basis_bps > 0.0
        basis_momentum_cooling = basis_same_sign and abs(mark_basis_bps) < abs(basis_bps)
        basis_persistence_status = "same_asset_persistent" if basis_same_sign else "mark_last_conflict"

        candidate = {
            "seen_at": seen_at,
            **_ticker_event_provenance(row, seen_at),
            "venue": "OKX",
            "inst_id": inst_id,
            "trade_type": "perp_funding_basis",
            "direction": direction,
            "execution_feasibility": feasibility,
            "thesis": thesis,
            "last": last,
            "index_px": idx_px,
            "mark_px": mark_px,
            "basis_bps": round(basis_bps, 3),
            "mark_basis_bps": round(mark_basis_bps, 3),
            "basis_bucket": _basis_bucket(basis_bps),
            **asset_context,
            "basis_persistence_status": basis_persistence_status,
            "basis_momentum_cooling": basis_momentum_cooling,
            "basis_mark_last_delta_bps": round(basis_bps - mark_basis_bps, 3),
            "funding_rate": funding_rate,
            "funding_bps": round(funding_bps, 3),
            "predicted_edge_bps": round(abs(funding_bps) + min(abs(basis_bps) * 0.45, 30.0), 3),
            "funding_interval_hours": funding_interval_hours,
            "next_funding_time": next_funding_time,
            "time_to_next_funding_minutes": _minutes_until(next_funding_time, decision_time),
            "change_24h_pct": round(change_24h_pct, 3),
            "quote_volume_24h": quote_volume,
            "open_interest_contracts": as_float(oi.get("oi"), None),
            "open_interest_ccy": as_float(oi.get("oiCcy"), None),
            "open_interest_usd": as_float(oi.get("oiUsd"), None),
            "contract_value": as_float(inst.get("ctVal"), None),
            "contract_value_ccy": inst.get("ctValCcy"),
            "settle_ccy": inst.get("settleCcy"),
            "instrument_state": inst.get("state"),
            "contract_type": inst.get("ctType"),
            **history,
            "liquidity_score": round(liquidity_score(quote_volume), 3),
            "spread_bps": round(spread_bps, 3),
            "quality_score": quality_score,
            "quality_status": quality_status,
            "risk_notes": [
                "paper-trade only",
                "funding can change before settlement",
                "basis can widen during momentum or liquidation cascades",
                "fees, borrow, spot leg availability, and venue risk are not fully modeled",
            ],
        }
        if signal_age_seconds is not None:
            candidate["signal_age_seconds"] = round(signal_age_seconds, 3)
        if direction == "short_perp_long_spot":
            candidate = apply_paired_direct_entry_contract(
                candidate,
                row,
                spot_ticker_by_inst.get(index_id),
                settings,
                decision_time=decision_time,
            )
        candidate["score"] = score_candidate(candidate)
        candidates.append(annotate_paper_context_cost(candidate, settings or {"mode": "paper"}))
        direction_counts[direction] += 1

    candidates.sort(key=lambda item: item["score"], reverse=True)
    candidate_by_inst = {str(item.get("inst_id")): item for item in candidates}
    observations = [
        _strategy_observation(
            row,
            candidate_by_inst.get(str(row.get("instId") or "")),
            instrument_by_inst.get(str(row.get("instId") or ""), {}),
            seen_at,
        )
        for _, row in usdt_swaps
        if as_float(row.get("last")) > 0
    ]
    return ScanBatch(
        source="OKX public REST",
        candidates=candidates,
        observations=observations,
        generated_at=seen_at,
        metadata={
            "priced_instrument_count": len(observations),
            "direction_counts": dict(direction_counts),
            "enriched_history_count": len(history_by_inst),
            "open_interest_count": len(open_interest_by_inst),
            "mark_price_count": len(mark_by_inst),
            "spot_ticker_count": len(spot_ticker_by_inst),
            "paired_direct_entry_complete_count": sum(
                1
                for item in candidates
                if item.get("paired_direct_contract_status") == "entry_complete"
            ),
            "paired_direct_incomplete_count": sum(
                1
                for item in candidates
                if item.get("paired_direct_contract_status") == "invalid_or_incomplete"
            ),
            "required_inst_id_count": len(required_inst_ids),
            "selected_instrument_count": len(selected),
        },
    )


def build_candidates(
    scan_universe: int,
    allow_short_spot: bool = False,
    settings: dict | None = None,
) -> list[dict]:
    return build_scan_batch(
        scan_universe,
        allow_short_spot=allow_short_spot,
        settings=settings,
    ).candidates


def write_outputs(candidates: list[dict]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "OKX public REST API",
        "mode": "research_paper_only",
        "candidates": candidates,
    }
    stamped_path = RUNS_DIR / f"opportunities_{stamp}.json"
    latest_path = RUNS_DIR / "latest_opportunities.json"
    stamped_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return stamped_path


def print_table(candidates: list[dict], limit: int) -> None:
    print("Rank | Score | Instrument | Direction | Fund bps | Basis bps | Spread bps | 24h % | Vol 24h")
    print("-" * 112)
    for idx, row in enumerate(candidates[:limit], start=1):
        print(
            f"{idx:>4} | {row['score']:>5.1f} | "
            f"{row['inst_id']:<18} | {row['direction']:<31} | "
            f"{row['funding_bps']:>8.3f} | {row['basis_bps']:>9.3f} | "
            f"{row['spread_bps']:>10.3f} | {row['change_24h_pct']:>6.2f} | "
            f"{row['quote_volume_24h']:>10.0f}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan OKX perp funding/basis dislocations.")
    parser.add_argument("--top", type=int, default=20, help="rows to print")
    parser.add_argument("--scan-universe", type=int, default=80, help="top-volume USDT swaps to scan")
    parser.add_argument(
        "--allow-short-spot",
        action="store_true",
        help="treat reverse cash-and-carry ideas as executable after external borrow confirmation",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        candidates = build_candidates(args.scan_universe, allow_short_spot=args.allow_short_spot)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1

    path = write_outputs(candidates)
    elapsed = time.perf_counter() - started
    print_table(candidates, args.top)
    print()
    print(f"Wrote {path}")
    print(f"Scanned {len(candidates)} instruments in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
