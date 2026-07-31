"""Shared paper-only normalization for public perpetual market adapters."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from frontier_data_quality import analyze_book


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def iso_from_epoch(value: Any, *, milliseconds: bool = True) -> str | None:
    number = as_float(value)
    if number is None:
        return None
    if milliseconds:
        number /= 1000.0
    try:
        return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def next_utc_funding_time(interval_hours: float = 8.0) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    interval_seconds = max(1, int(float(interval_hours) * 3600.0))
    next_epoch = ((int(now.timestamp()) // interval_seconds) + 1) * interval_seconds
    return dt.datetime.fromtimestamp(next_epoch, tz=dt.timezone.utc).isoformat()


def liquidity_score(quote_volume: Any) -> float:
    volume = max(0.0, float(as_float(quote_volume, 0.0) or 0.0))
    if volume <= 0:
        return 0.0
    return round(max(0.0, min(1.0, (math.log10(volume) - 5.0) / 4.0)), 3)


def spread_bps(bid: Any, ask: Any, last: Any) -> float:
    bid_value = float(as_float(bid, 0.0) or 0.0)
    ask_value = float(as_float(ask, 0.0) or 0.0)
    last_value = float(as_float(last, 0.0) or 0.0)
    mid = (bid_value + ask_value) / 2.0 if bid_value > 0 and ask_value > 0 else last_value
    if ask_value > bid_value > 0 and mid > 0:
        return round((ask_value - bid_value) / mid * 10_000.0, 3)
    return 999.0


def enrich_book(observation: dict, payload: dict, latency_ms: float, received_at: str) -> dict:
    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    raw = {
        "bids": body.get("bids") or [],
        "asks": body.get("asks") or [],
        "book_timestamp": iso_from_epoch(body.get("timestamp")) or received_at,
        "freshness_basis": "exchange_timestamp" if body.get("timestamp") else "response_received",
    }
    return {
        **observation,
        **analyze_book(
            observation,
            raw,
            latency_ms=float(latency_ms),
            received_at=received_at,
            max_levels=50,
        ),
    }


def funding_candidate(observation: dict, settings: dict) -> dict | None:
    funding_bps = float(as_float(observation.get("funding_bps"), 0.0) or 0.0)
    if funding_bps == 0:
        return None
    direction = "funding_capture_short_perp" if funding_bps > 0 else "funding_capture_long_perp"
    quality_score = as_float(observation.get("quality_score"))
    quality_status = str(observation.get("quality_status") or "unknown")
    risk = settings.get("risk") or {}
    adapter_cfg = settings.get("derivatives_market_adapters") or {}
    min_quality = float(adapter_cfg.get("min_quality_score", 60.0))
    fee = float(adapter_cfg.get("fee_bps_per_side", risk.get("taker_fee_bps_per_leg", 5.0)))
    fallback_slippage = float(adapter_cfg.get("slippage_bps_per_side", risk.get("slippage_bps_per_leg", 3.0)))
    fills = observation.get("simulated_fills") or {}
    entry_side = "sell" if direction == "funding_capture_short_perp" else "buy"
    exit_side = "buy" if entry_side == "sell" else "sell"
    entry_slippage = (((fills.get(entry_side) or {}).get("1000") or {}).get("slippage_bps"))
    exit_slippage = (((fills.get(exit_side) or {}).get("1000") or {}).get("slippage_bps"))
    entry_slippage = fallback_slippage if entry_slippage is None else float(entry_slippage)
    exit_slippage = fallback_slippage if exit_slippage is None else float(exit_slippage)
    round_trip_cost = fee * 2.0 + entry_slippage + exit_slippage
    expected_funding = abs(funding_bps)
    net_edge = expected_funding - round_trip_cost
    min_funding = float(adapter_cfg.get("min_abs_funding_bps", 3.0))
    min_edge = float(adapter_cfg.get("min_net_carry_edge_bps", risk.get("min_net_edge_bps", 2.0)))
    if (
        abs(funding_bps) < min_funding
        or net_edge < min_edge
        or quality_status not in {"verified", "degraded"}
        or quality_score is None
        or quality_score < min_quality
    ):
        return None
    last = float(observation["last"])
    index_price = float(as_float(observation.get("index_price"), last) or last)
    basis_bps = (last / index_price - 1.0) * 10_000.0 if index_price > 0 else 0.0
    capabilities = settings.get("account_capabilities") or {}
    route_ready = bool(capabilities.get("crypto_derivatives", False))
    liquidity = liquidity_score(observation.get("quote_volume_24h"))
    spread = spread_bps(observation.get("bid"), observation.get("ask"), last)
    score = max(
        0.0,
        min(100.0, 50.0 + min(25.0, net_edge) + (quality_score - 60.0) * 0.25 + liquidity * 10.0),
    )
    interval_hours = float(as_float(observation.get("funding_interval_hours"), 8.0) or 8.0)
    return {
        "seen_at": observation.get("observed_at"),
        "observed_at": observation.get("observed_at"),
        "venue": observation["venue"],
        "inst_id": observation["inst_id"],
        "symbol": observation["symbol"],
        "base": observation.get("base"),
        "quote": observation.get("quote"),
        "asset_class": "crypto_derivatives",
        "market_surface": "public_perpetual_funding_carry",
        "trade_type": "perp_funding_basis",
        "strategy_lineage_key": "public_perpetual_funding_capture_v1",
        "signal_lineage_key": "public_perpetual_funding_capture_v1",
        "direction": direction,
        "thesis": "Collect the next public perpetual funding payment only when it exceeds conservative round-trip costs.",
        "last": last,
        "bid": observation.get("bid"),
        "ask": observation.get("ask"),
        "index_price": index_price,
        "funding_rate": observation.get("funding_rate"),
        "funding_bps": round(funding_bps, 3),
        "funding_interval_hours": interval_hours,
        "next_funding_time": observation.get("next_funding_time") or next_utc_funding_time(interval_hours),
        "expected_funding_bps_to_next": round(expected_funding, 3),
        "expected_funding_bps_per_day": round(expected_funding * 24.0 / interval_hours, 3),
        "basis_bps": round(basis_bps, 3),
        "mark_basis_bps": round(basis_bps, 3),
        "edge_bps_estimate": round(net_edge, 3),
        "net_carry_edge_bps": round(net_edge, 3),
        "gross_edge_bps_estimate": round(expected_funding, 3),
        "estimated_round_trip_cost_bps": round(round_trip_cost, 3),
        "round_trip_cost_bps": round(round_trip_cost, 3),
        "estimated_fee_bps_per_side": round(fee, 3),
        "entry_slippage_bps_estimate": round(entry_slippage, 3),
        "exit_slippage_bps_estimate": round(exit_slippage, 3),
        "carry_alignment_status": "carry_aligned_positive",
        "change_24h_pct": float(as_float(observation.get("change_24h_pct"), 0.0) or 0.0),
        "quote_volume_24h": float(as_float(observation.get("quote_volume_24h"), 0.0) or 0.0),
        "liquidity_score": liquidity,
        "spread_bps": spread,
        "score": round(score, 3),
        "data_status": observation.get("data_status", "reachable"),
        "quality_status": quality_status,
        "quality_score": quality_score,
        "quality_action": "normal",
        "quality_allocation_multiplier": 1.0,
        "source_venue_count": 1,
        "paper_entry_blocked": False,
        "promotion_eligible": True,
        "simulated_fills": fills,
        "anomaly_flags": list(observation.get("anomaly_flags") or []),
        "critical_anomaly_flags": list(observation.get("critical_anomaly_flags") or []),
        "execution_feasibility": {
            "status": "standard" if route_ready else "conditional",
            "requires_short_spot": False,
            "route_blockers": [] if route_ready else ["crypto_derivatives"],
            "notes": ["Public-data perpetual funding candidate; paper execution only."],
        },
        "data_source": {
            "provider": f"{observation['venue']} public REST",
            "url": observation.get("source_url"),
            "data_status": observation.get("data_status", "reachable"),
        },
    }
