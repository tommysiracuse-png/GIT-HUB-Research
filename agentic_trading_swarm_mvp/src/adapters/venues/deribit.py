"""Deribit public derivatives observations and paper funding candidates."""

from __future__ import annotations

import concurrent.futures
import urllib.parse

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, parse_json, utc_now
from adapters.venues.crypto_derivatives import (
    as_float,
    funding_candidate,
    iso_from_epoch,
    liquidity_score,
    spread_bps,
)
from frontier_data_quality import analyze_book
from scan_batch import ScanBatch


API_ROOT = "https://www.deribit.com/api/v2/public"
SUMMARY_URL = API_ROOT + "/get_book_summary_by_currency?currency={currency}&kind={kind}"
ORDER_BOOK_URL = API_ROOT + "/get_order_book?instrument_name={instrument}&depth={depth}"
DOCS_URL = "https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency"


def parse_deribit_summaries(
    payload: object,
    *,
    kind: str,
    observed_at: str | None = None,
    source_url: str = SUMMARY_URL,
) -> list[dict]:
    body = payload if isinstance(payload, dict) else {}
    rows = body.get("result") if isinstance(body.get("result"), list) else []
    timestamp = observed_at or utc_now()
    output: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("instrument_name") or "").upper()
        mark = as_float(item.get("mark_price"))
        last = as_float(item.get("last"), mark)
        if not symbol or last is None or last <= 0:
            continue
        base = str(item.get("base_currency") or symbol.split("-", 1)[0]).upper()
        quote = str(item.get("quote_currency") or "USD").upper()
        bid = as_float(item.get("bid_price"))
        ask = as_float(item.get("ask_price"))
        is_perpetual = symbol.endswith("-PERPETUAL")
        funding_rate = as_float(item.get("funding_8h"), 0.0) or 0.0
        research_trade_type = (
            "perp_funding_basis"
            if is_perpetual
            else "crypto_futures_curve_research"
            if kind == "future"
            else "crypto_options_volatility_research"
        )
        row = {
            "venue": "DERIBIT",
            "inst_id": f"DERIBIT:{symbol}",
            "instrument_id": f"DERIBIT:{symbol}",
            "symbol": symbol,
            "base": base,
            "quote": quote,
            "market_type": "perp" if is_perpetual else kind,
            "market_surface": "deribit_public_perpetuals" if is_perpetual else "deribit_public_options_or_futures",
            "asset_class": "crypto_derivatives" if kind == "future" else "crypto_options",
            "trade_type": research_trade_type,
            "direction": "watch_only",
            "last": last,
            "mark_price": mark,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps(bid, ask, last),
            "index_price": as_float(item.get("estimated_delivery_price")) or as_float(item.get("underlying_price")) or last,
            "funding_rate": funding_rate if is_perpetual else 0.0,
            "funding_bps": funding_rate * 10_000.0 if is_perpetual else 0.0,
            "funding_interval_hours": 8.0 if is_perpetual else None,
            "quote_volume_24h": as_float(item.get("volume_usd"), 0.0) or 0.0,
            "open_interest": as_float(item.get("open_interest"), 0.0) or 0.0,
            "change_24h_pct": as_float(item.get("price_change"), 0.0) or 0.0,
            "liquidity_score": liquidity_score(item.get("volume_usd")),
            "mark_iv": as_float(item.get("mark_iv")),
            "underlying_price": as_float(item.get("underlying_price")),
            "exchange_timestamp": iso_from_epoch(item.get("creation_timestamp")),
            "data_status": "reachable",
            "quality_status": "unknown",
            "quality_score": None,
            "session_status": "open_24_7",
            "observed_at": timestamp,
            "price_source": "Deribit public book summary",
            "source_url": source_url,
            "candidate_reject_reason": (
                "depth_and_net_carry_gate_pending"
                if is_perpetual
                else "term_structure_strategy_hypothesis_required"
                if kind == "future"
                else "options_strategy_hypothesis_required"
            ),
        }
        output.append(row)
    output.sort(key=lambda row: float(row.get("quote_volume_24h") or 0.0), reverse=True)
    return output


def _normalize_deribit_book(payload: dict) -> dict:
    """Convert Deribit's USD-contract quantities to base-equivalent quantities."""

    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload

    def convert(rows: object) -> list[list[float]]:
        converted: list[list[float]] = []
        for level in rows if isinstance(rows, list) else []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price = as_float(level[0])
            notional = as_float(level[1])
            if price and price > 0 and notional and notional > 0:
                converted.append([price, notional / price])
        return converted

    return {
        "bids": convert(body.get("bids")),
        "asks": convert(body.get("asks")),
        "book_timestamp": iso_from_epoch(body.get("timestamp")) or utc_now(),
        "freshness_basis": "exchange_timestamp" if body.get("timestamp") else "response_received",
    }


def _fetch_book(observation: dict, timeout: int, depth: int) -> dict:
    url = ORDER_BOOK_URL.format(
        instrument=urllib.parse.quote(str(observation["symbol"]), safe="-_"),
        depth=max(1, min(50, int(depth))),
    )
    result = fetch_text(url, timeout)
    if not result["ok"]:
        return {
            **observation,
            "depth_source_url": url,
            "quality_status": "blocked" if result["status"] == "blocked" else "unknown",
            "quality_score": None,
            "anomaly_flags": ["order_book_unavailable"],
            "critical_anomaly_flags": [],
            "candidate_reject_reason": "public_order_book_unavailable",
        }
    try:
        payload = parse_json(result["text"])
        raw_book = _normalize_deribit_book(payload)
    except (TypeError, ValueError):
        return {
            **observation,
            "depth_source_url": url,
            "quality_status": "unknown",
            "quality_score": None,
            "anomaly_flags": ["malformed_order_book"],
            "critical_anomaly_flags": [],
            "candidate_reject_reason": "malformed_public_order_book",
        }
    enriched = {
        **observation,
        **analyze_book(
            observation,
            raw_book,
            latency_ms=float(result["latency_ms"]),
            received_at=result["received_at"],
            max_levels=max(1, min(50, int(depth))),
        ),
    }
    enriched["depth_source_url"] = url
    enriched["candidate_reject_reason"] = "net_carry_gate_pending"
    return enriched


class DeribitDerivativesAdapter:
    info = AdapterInfo(
        adapter_id="deribit_derivatives_public",
        venue="DERIBIT",
        market_type="derivatives",
        source="Deribit official public JSON-RPC REST",
        capabilities=(
            "ticker",
            "order_book",
            "funding",
            "index_price",
            "open_interest",
            "options_volatility",
            "executable_quality",
            "candidate_generation",
        ),
        aliases=("deribit", "deribit perpetual", "deribit options"),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.deribit.DeribitDerivativesAdapter",
        quote_assets=("USD", "USDC", "BTC", "ETH"),
        default_cache_minutes=2,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        settings = settings or {}
        cfg = ((settings.get("public_market_adapters") or {}).get(self.info.adapter_id) or {})
        timeout = int(cfg.get("timeout_seconds", 12))
        currencies = [str(item).upper() for item in cfg.get("currencies", ["BTC", "ETH", "USDC"])]
        requests = [(currency, kind) for currency in currencies for kind in ("future", "option")]
        results: list[tuple[str, str, dict]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(requests) or 1)) as pool:
            futures = {
                pool.submit(fetch_text, SUMMARY_URL.format(currency=currency, kind=kind), timeout): (currency, kind)
                for currency, kind in requests
            }
            for future in concurrent.futures.as_completed(futures):
                currency, kind = futures[future]
                try:
                    results.append((currency, kind, future.result()))
                except Exception:  # noqa: BLE001 - retain other public currencies.
                    continue

        observations: list[dict] = []
        statuses: list[str] = []
        option_cap = max(0, int(cfg.get("max_options_per_currency", 40)))
        future_cap = max(1, int(cfg.get("max_futures_per_currency", 40)))
        for currency, kind, result in results:
            statuses.append(str(result.get("status") or "unavailable"))
            if not result.get("ok"):
                continue
            url = SUMMARY_URL.format(currency=currency, kind=kind)
            try:
                rows = parse_deribit_summaries(
                    parse_json(result["text"]),
                    kind=kind,
                    observed_at=result["received_at"],
                    source_url=url,
                )
            except (TypeError, ValueError):
                continue
            observations.extend(rows[: option_cap if kind == "option" else future_cap])

        perps = [row for row in observations if row.get("market_type") == "perp"]
        max_depth = max(0, min(int(cfg.get("max_depth_books", 6)), len(perps)))
        enriched_by_id: dict[str, dict] = {}
        if max_depth:
            selected = sorted(
                perps,
                key=lambda row: (abs(float(row.get("funding_bps") or 0.0)), float(row.get("quote_volume_24h") or 0.0)),
                reverse=True,
            )[:max_depth]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
                futures = {
                    pool.submit(_fetch_book, row, timeout, int(cfg.get("depth_levels", 20))): row["inst_id"]
                    for row in selected
                }
                for future in concurrent.futures.as_completed(futures):
                    try:
                        enriched_by_id[futures[future]] = future.result()
                    except Exception:  # noqa: BLE001 - one instrument must not stop radar.
                        continue
        observations = [enriched_by_id.get(row["inst_id"], row) for row in observations]
        candidates = [
            candidate
            for row in observations
            if row.get("market_type") == "perp" and (candidate := funding_candidate(row, settings))
        ]
        if not observations:
            fallback = next((item[2] for item in results), {"status": "unavailable", "received_at": utc_now()})
            observations = [health_observation("DERIBIT", DOCS_URL, fallback, "deribit_public_derivatives")]
        source_status = "reachable" if "reachable" in statuses else (statuses[0] if statuses else "unavailable")
        return ScanBatch(
            source=self.info.source,
            candidates=candidates,
            observations=observations,
            metadata={
                "source_status": source_status,
                "observation_count": len(observations),
                "perpetual_count": sum(1 for row in observations if row.get("market_type") == "perp"),
                "option_count": sum(1 for row in observations if row.get("asset_class") == "crypto_options"),
                "depth_count": len(enriched_by_id),
                "candidate_count": len(candidates),
                "paper_only": True,
            },
        )


register_adapter(DeribitDerivativesAdapter())
