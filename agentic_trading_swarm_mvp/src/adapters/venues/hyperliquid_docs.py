"""Hyperliquid public perpetual funding and open-interest-cap observations.

This adapter intentionally uses only the unauthenticated ``/info`` endpoint.  It
collects funding diagnostics for paper research; it does not expose an order,
account, or broker operation.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, parse_json, utc_now
from adapters.venues.crypto_derivatives import as_float, iso_from_epoch
from scan_batch import ScanBatch


INFO_URL = "https://api.hyperliquid.xyz/info"
PERPETUALS_DOCS_URL = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/"
    "info-endpoint/perpetuals"
)
RATE_LIMITS_DOCS_URL = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/"
    "rate-limits-and-user-limits"
)
HIP3_DOCS_URL = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/"
    "hip-3-deployer-actions"
)


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _funding_history_observation(
    coin: str,
    latest: dict[str, Any],
    *,
    history_count: int,
    observed_at: str,
    source_url: str,
) -> dict[str, Any]:
    funding_rate = as_float(latest.get("fundingRate"), 0.0) or 0.0
    timestamp = iso_from_epoch(latest.get("time"))
    return {
        "venue": "HYPERLIQUID",
        "inst_id": f"HYPERLIQUID:{coin}-PERP",
        "instrument_id": f"HYPERLIQUID:{coin}-PERP",
        "symbol": f"{coin}-PERP",
        "base": coin,
        "quote": "USD",
        "market_type": "perp",
        "market_surface": "hyperliquid_public_funding_history",
        "asset_class": "crypto_derivatives",
        "trade_type": "perp_funding_divergence_research",
        "direction": "watch_only",
        "realized_funding_rate": funding_rate,
        "realized_funding_bps": funding_rate * 10_000.0,
        "realized_premium": as_float(latest.get("premium")),
        "funding_history_count": history_count,
        "funding_interval_hours": 1.0,
        "exchange_timestamp": timestamp,
        "data_status": "reachable",
        "freshness_state": "fresh" if timestamp else "response_received",
        "freshness_basis": "exchange_timestamp" if timestamp else "response_received",
        "session_status": "open_24_7",
        "observed_at": observed_at,
        "price_source": "Hyperliquid public fundingHistory",
        "source_url": source_url,
        "api_endpoint": INFO_URL,
        "request_type": "fundingHistory",
        "candidate_reject_reason": "funding_divergence_requires_priceable_cross_venue_legs",
    }


def parse_hyperliquid_funding_history(
    payload: object,
    *,
    coin: str,
    observed_at: str | None = None,
    source_url: str = PERPETUALS_DOCS_URL,
) -> dict[str, Any] | None:
    """Return the most recent realized funding observation for one perpetual."""

    if not isinstance(payload, list):
        raise ValueError("fundingHistory response must be a list")
    requested_coin = str(coin).upper()
    rows = [
        row
        for row in payload
        if isinstance(row, dict) and str(row.get("coin") or "").upper() == requested_coin
    ]
    if not rows:
        return None
    latest = max(rows, key=lambda row: float(as_float(row.get("time"), 0.0) or 0.0))
    if as_float(latest.get("fundingRate")) is None:
        raise ValueError(f"fundingHistory {requested_coin} has no numeric fundingRate")
    return _funding_history_observation(
        requested_coin,
        latest,
        history_count=len(rows),
        observed_at=observed_at or utc_now(),
        source_url=source_url,
    )


def _predicted_venue_rates(rows: object) -> dict[str, dict[str, float | str | None]]:
    if not isinstance(rows, list):
        raise ValueError("predictedFundings venue rows must be a list")
    output: dict[str, dict[str, float | str | None]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        venue, details = row
        if not isinstance(details, dict):
            continue
        rate = as_float(details.get("fundingRate"))
        if rate is None:
            continue
        interval = as_float(details.get("fundingIntervalHours"))
        output[str(venue)] = {
            "funding_rate": rate,
            "funding_bps": rate * 10_000.0,
            "funding_interval_hours": interval,
            "next_funding_time": iso_from_epoch(details.get("nextFundingTime")),
            "funding_bps_per_hour": rate * 10_000.0 / interval if interval and interval > 0 else None,
        }
    return output


def parse_hyperliquid_predicted_fundings(
    payload: object,
    *,
    coins: set[str] | None = None,
    observed_at: str | None = None,
    source_url: str = PERPETUALS_DOCS_URL,
) -> dict[str, dict[str, Any]]:
    """Normalize Hyperliquid's public forecast and comparable external venue rates."""

    if not isinstance(payload, list):
        raise ValueError("predictedFundings response must be a list")
    requested = {str(coin).upper() for coin in coins} if coins else None
    output: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        coin = str(item[0] or "").upper()
        if not coin or (requested is not None and coin not in requested):
            continue
        venue_rates = _predicted_venue_rates(item[1])
        hyperliquid = venue_rates.get("HlPerp")
        if hyperliquid is None:
            continue
        hl_hourly = hyperliquid.get("funding_bps_per_hour")
        comparable_deltas = [
            float(rate["funding_bps_per_hour"]) - float(hl_hourly)
            for venue, rate in venue_rates.items()
            if venue != "HlPerp" and rate.get("funding_bps_per_hour") is not None and hl_hourly is not None
        ]
        largest_delta = max(comparable_deltas, key=abs) if comparable_deltas else None
        output[coin] = {
            "venue": "HYPERLIQUID",
            "inst_id": f"HYPERLIQUID:{coin}-PERP",
            "instrument_id": f"HYPERLIQUID:{coin}-PERP",
            "symbol": f"{coin}-PERP",
            "base": coin,
            "quote": "USD",
            "market_type": "perp",
            "market_surface": "hyperliquid_public_predicted_fundings",
            "asset_class": "crypto_derivatives",
            "trade_type": "perp_funding_divergence_research",
            "direction": "watch_only",
            "predicted_funding_rate": hyperliquid["funding_rate"],
            "predicted_funding_bps": hyperliquid["funding_bps"],
            "predicted_funding_bps_per_hour": hl_hourly,
            "funding_interval_hours": hyperliquid["funding_interval_hours"],
            "next_funding_time": hyperliquid["next_funding_time"],
            "predicted_funding_venue_rates": venue_rates,
            "external_funding_venue_count": max(0, len(venue_rates) - 1),
            "largest_external_funding_divergence_bps_per_hour": largest_delta,
            "data_status": "reachable",
            "freshness_state": "fresh",
            "freshness_basis": "response_received",
            "session_status": "open_24_7",
            "observed_at": observed_at or utc_now(),
            "price_source": "Hyperliquid public predictedFundings",
            "source_url": source_url,
            "api_endpoint": INFO_URL,
            "request_type": "predictedFundings",
            "candidate_reject_reason": "funding_divergence_requires_priceable_cross_venue_legs",
        }
    return output


def parse_hyperliquid_open_interest_caps(
    payload: object,
    *,
    dex: str | None = None,
    observed_at: str | None = None,
    source_url: str = PERPETUALS_DOCS_URL,
) -> dict[str, Any]:
    """Normalize a per-dex public capacity check, including the useful empty result."""

    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise ValueError("perpsAtOpenInterestCap response must be a list of symbols")
    dex_name = str(dex or "main")
    capped = sorted({str(item).upper() for item in payload if str(item).strip()})
    return {
        "venue": "HYPERLIQUID",
        "inst_id": f"HYPERLIQUID:{dex_name}:PERPS_AT_OPEN_INTEREST_CAP",
        "instrument_id": f"HYPERLIQUID:{dex_name}:PERPS_AT_OPEN_INTEREST_CAP",
        "symbol": "PERPS_AT_OPEN_INTEREST_CAP",
        "base": "PERPS_AT_OPEN_INTEREST_CAP",
        "quote": "N/A",
        "market_type": "reference",
        "market_surface": "hyperliquid_public_open_interest_cap",
        "asset_class": "crypto_derivatives",
        "trade_type": "perp_capacity_diagnostic",
        "direction": "watch_only",
        "perp_dex": dex_name,
        "open_interest_cap_symbols": capped,
        "open_interest_cap_count": len(capped),
        "data_status": "reachable",
        "freshness_state": "fresh",
        "freshness_basis": "response_received",
        "session_status": "open_24_7",
        "observed_at": observed_at or utc_now(),
        "price_source": "Hyperliquid public perpsAtOpenInterestCap",
        "source_url": source_url,
        "api_endpoint": INFO_URL,
        "request_type": "perpsAtOpenInterestCap",
        "candidate_reject_reason": "capacity_diagnostic_not_a_priceable_instrument",
    }


def _fetch_evidence(result: dict[str, Any], source_url: str, request_body: dict[str, Any]) -> dict[str, Any]:
    return {
        "fetch_status": result.get("status", "unavailable"),
        "http_status": result.get("http_status"),
        "received_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "source_url": source_url,
        "api_endpoint": INFO_URL,
        "request_body": request_body,
        "error": result.get("error"),
    }


def _failure_observation(
    surface: str,
    result: dict[str, Any],
    *,
    source_url: str,
    request_type: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    status = str(result.get("status") or "unavailable")
    return {
        "venue": "HYPERLIQUID",
        "inst_id": f"HYPERLIQUID:{surface}:SOURCE_EVIDENCE",
        "instrument_id": f"HYPERLIQUID:{surface}:SOURCE_EVIDENCE",
        "symbol": "SOURCE_EVIDENCE",
        "base": "SOURCE_EVIDENCE",
        "quote": "N/A",
        "market_type": "reference",
        "market_surface": "hyperliquid_public_source_health",
        "asset_class": "market_data_health",
        "trade_type": "official_market_reference",
        "direction": "watch_only",
        "last": 0.0,
        "data_status": status,
        "http_status": result.get("http_status"),
        "freshness_state": "unknown",
        "session_status": "unknown",
        "observed_at": result.get("received_at") or utc_now(),
        "source_url": source_url,
        "api_endpoint": INFO_URL,
        "request_type": request_type,
        "candidate_reject_reason": (
            "public_perpetuals_parser_failure" if parser_error else "public_perpetuals_source_unavailable"
        ),
        "notes": [parser_error or str(result.get("error") or "Public source did not return usable data.")],
    }


class HyperliquidPublicPerpetualsAdapter:
    info = AdapterInfo(
        adapter_id="hyperliquid_public_perpetuals",
        venue="HYPERLIQUID",
        market_type="perp",
        source="Hyperliquid official public Info API",
        capabilities=(
            "funding_history",
            "predicted_fundings",
            "cross_venue_funding_diagnostics",
            "open_interest_cap",
            "hip3_perp_dex_capacity",
            "source_health",
            "paper_research",
        ),
        aliases=("hyperliquid", "hyperliquid perps", "hyperliquid funding"),
        docs_url=PERPETUALS_DOCS_URL,
        runtime_entrypoint="adapters.venues.hyperliquid_docs.HyperliquidPublicPerpetualsAdapter",
        quote_assets=("USD", "USDC"),
        default_cache_minutes=2,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 12)))
        coins = [str(item).upper() for item in cfg.get("coins", ["ETH", "AVAX"]) if str(item).strip()]
        coins = list(dict.fromkeys(coins)) or ["ETH", "AVAX"]
        history_hours = max(1.0, float(cfg.get("history_hours", 12.0)))
        start_time = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=history_hours)).timestamp() * 1000)
        dexes = cfg.get("perp_dexes", [None])
        if not isinstance(dexes, list):
            dexes = [dexes]
        normalized_dexes = list(dict.fromkeys(str(item).strip() if item else None for item in dexes)) or [None]

        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        history_rows: dict[str, dict[str, Any]] = {}

        for coin in coins:
            request = {"type": "fundingHistory", "coin": coin, "startTime": start_time}
            result = fetch_text(INFO_URL, timeout, method="POST", json_body=request)
            key = f"funding_history:{coin}"
            fetch_status[key] = _fetch_evidence(result, PERPETUALS_DOCS_URL, request)
            if not result.get("ok"):
                observations.append(_failure_observation(key, result, source_url=PERPETUALS_DOCS_URL, request_type="fundingHistory"))
                continue
            try:
                row = parse_hyperliquid_funding_history(
                    parse_json(result["text"]), coin=coin, observed_at=result.get("received_at")
                )
                if row is not None:
                    history_rows[coin] = row
            except (TypeError, ValueError) as exc:
                message = f"fundingHistory {coin} parser failed: {exc}"[:300]
                parser_failures.append({"surface": key, "source_url": PERPETUALS_DOCS_URL, "error": message})
                observations.append(_failure_observation(key, result, source_url=PERPETUALS_DOCS_URL, request_type="fundingHistory", parser_error=message))

        predicted_request = {"type": "predictedFundings"}
        predicted_result = fetch_text(INFO_URL, timeout, method="POST", json_body=predicted_request)
        fetch_status["predicted_fundings"] = _fetch_evidence(predicted_result, PERPETUALS_DOCS_URL, predicted_request)
        predicted_rows: dict[str, dict[str, Any]] = {}
        if not predicted_result.get("ok"):
            observations.append(_failure_observation("predicted_fundings", predicted_result, source_url=PERPETUALS_DOCS_URL, request_type="predictedFundings"))
        else:
            try:
                predicted_rows = parse_hyperliquid_predicted_fundings(
                    parse_json(predicted_result["text"]), coins=set(coins), observed_at=predicted_result.get("received_at")
                )
            except (TypeError, ValueError) as exc:
                message = f"predictedFundings parser failed: {exc}"[:300]
                parser_failures.append({"surface": "predicted_fundings", "source_url": PERPETUALS_DOCS_URL, "error": message})
                observations.append(_failure_observation("predicted_fundings", predicted_result, source_url=PERPETUALS_DOCS_URL, request_type="predictedFundings", parser_error=message))

        for coin in coins:
            merged = {**predicted_rows.get(coin, {}), **history_rows.get(coin, {})}
            if predicted_rows.get(coin) and history_rows.get(coin):
                merged["market_surface"] = "hyperliquid_public_funding_history_and_predictions"
                merged["predicted_funding_venue_rates"] = predicted_rows[coin]["predicted_funding_venue_rates"]
                merged["external_funding_venue_count"] = predicted_rows[coin]["external_funding_venue_count"]
                merged["largest_external_funding_divergence_bps_per_hour"] = predicted_rows[coin]["largest_external_funding_divergence_bps_per_hour"]
                merged["predicted_funding_rate"] = predicted_rows[coin]["predicted_funding_rate"]
                merged["predicted_funding_bps"] = predicted_rows[coin]["predicted_funding_bps"]
                merged["predicted_funding_bps_per_hour"] = predicted_rows[coin]["predicted_funding_bps_per_hour"]
            if merged:
                observations.append(merged)

        for dex in normalized_dexes:
            request = {"type": "perpsAtOpenInterestCap"}
            if dex:
                request["dex"] = dex
            result = fetch_text(INFO_URL, timeout, method="POST", json_body=request)
            dex_name = str(dex or "main")
            key = f"open_interest_cap:{dex_name}"
            source_url = HIP3_DOCS_URL if dex else PERPETUALS_DOCS_URL
            fetch_status[key] = _fetch_evidence(result, source_url, request)
            if not result.get("ok"):
                observations.append(_failure_observation(key, result, source_url=source_url, request_type="perpsAtOpenInterestCap"))
                continue
            try:
                observations.append(
                    parse_hyperliquid_open_interest_caps(
                        parse_json(result["text"]), dex=dex, observed_at=result.get("received_at"), source_url=source_url
                    )
                )
            except (TypeError, ValueError) as exc:
                message = f"perpsAtOpenInterestCap {dex_name} parser failed: {exc}"[:300]
                parser_failures.append({"surface": key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(key, result, source_url=source_url, request_type="perpsAtOpenInterestCap", parser_error=message))

        statuses = [str(item["fetch_status"]) for item in fetch_status.values()]
        real_rows = [row for row in observations if row.get("data_status") == "reachable"]
        if real_rows and not parser_failures and all(status == "reachable" for status in statuses):
            source_status = "reachable"
        elif real_rows:
            source_status = "degraded"
        elif statuses and all(status == "blocked" for status in statuses):
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1208,
                "source_status": source_status,
                "source_urls": [PERPETUALS_DOCS_URL, RATE_LIMITS_DOCS_URL, HIP3_DOCS_URL],
                "api_endpoint": INFO_URL,
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "paper_only": True,
                "live_trading_enabled": False,
            },
        )


register_adapter(HyperliquidPublicPerpetualsAdapter())
