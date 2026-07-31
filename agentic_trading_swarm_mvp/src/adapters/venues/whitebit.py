"""WhiteBIT public perpetual market-data and paper-carry adapter."""

from __future__ import annotations

import concurrent.futures
import urllib.parse

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, parse_json, utc_now
from adapters.venues.crypto_derivatives import (
    as_float,
    enrich_book,
    funding_candidate,
    iso_from_epoch,
    liquidity_score,
    spread_bps,
)
from scan_batch import ScanBatch


FUTURES_URL = "https://whitebit.com/api/v4/public/futures"
ORDER_BOOK_URL = "https://whitebit.com/api/v4/public/orderbook/{market}?limit={limit}&level=0"
DOCS_URL = "https://docs.whitebit.com/api-reference/market-data/available-futures-markets-list"


def parse_whitebit_futures(
    payload: object,
    *,
    observed_at: str | None = None,
    source_url: str = FUTURES_URL,
) -> list[dict]:
    """Normalize the official all-futures response without inventing prices."""

    body = payload if isinstance(payload, dict) else {}
    rows = body.get("result") if isinstance(body.get("result"), list) else []
    timestamp = observed_at or utc_now()
    output: list[dict] = []
    for item in rows:
        if not isinstance(item, dict) or str(item.get("product_type") or "").lower() != "perpetual":
            continue
        symbol = str(item.get("ticker_id") or "").upper()
        last = as_float(item.get("last_price"))
        if not symbol or last is None or last <= 0:
            continue
        base = str(item.get("stock_currency") or symbol.removesuffix("_PERP")).upper()
        quote = str(item.get("money_currency") or "USDT").upper()
        bid = as_float(item.get("bid"))
        ask = as_float(item.get("ask"))
        funding_rate = as_float(item.get("funding_rate"), 0.0) or 0.0
        interval_minutes = as_float(item.get("funding_interval_minutes"), 480.0) or 480.0
        output.append(
            {
                "venue": "WHITEBIT",
                "inst_id": f"WHITEBIT:{symbol}",
                "instrument_id": f"WHITEBIT:{symbol}",
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "market_type": "perp",
                "market_surface": "whitebit_public_perpetuals",
                "asset_class": "crypto_derivatives",
                "trade_type": "perp_funding_basis",
                "direction": "watch_only",
                "last": last,
                "bid": bid,
                "ask": ask,
                "spread_bps": spread_bps(bid, ask, last),
                "index_price": as_float(item.get("index_price"), last) or last,
                "funding_rate": funding_rate,
                "funding_bps": funding_rate * 10_000.0,
                "funding_interval_hours": interval_minutes / 60.0,
                "next_funding_time": iso_from_epoch(item.get("next_funding_rate_timestamp")),
                "quote_volume_24h": as_float(item.get("money_volume"), 0.0) or 0.0,
                "open_interest": as_float(item.get("open_interest"), 0.0) or 0.0,
                "change_24h_pct": 0.0,
                "liquidity_score": liquidity_score(item.get("money_volume")),
                "data_status": "reachable",
                "quality_status": "unknown",
                "quality_score": None,
                "session_status": "open_24_7",
                "observed_at": timestamp,
                "price_source": "WhiteBIT public futures",
                "source_url": source_url,
                "candidate_reject_reason": "depth_and_net_carry_gate_pending",
            }
        )
    output.sort(
        key=lambda row: (float(row.get("quote_volume_24h") or 0.0), abs(float(row.get("funding_bps") or 0.0))),
        reverse=True,
    )
    return output


def _fetch_book(observation: dict, timeout: int, levels: int) -> dict:
    url = ORDER_BOOK_URL.format(
        market=urllib.parse.quote(str(observation["symbol"]), safe="-_"),
        limit=max(1, min(100, int(levels))),
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
    enriched = enrich_book(observation, payload, result["latency_ms"], result["received_at"])
    enriched["depth_source_url"] = url
    enriched["candidate_reject_reason"] = "net_carry_gate_pending"
    return enriched


class WhitebitPerpetualAdapter:
    info = AdapterInfo(
        adapter_id="whitebit_perpetuals_public",
        venue="WHITEBIT",
        market_type="perp",
        source="WhiteBIT official public REST",
        capabilities=(
            "ticker",
            "order_book",
            "funding",
            "index_price",
            "open_interest",
            "executable_quality",
            "candidate_generation",
        ),
        aliases=("whitebit", "whitebit perpetual", "whitebit futures"),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.whitebit.WhitebitPerpetualAdapter",
        quote_assets=("USDT",),
        default_cache_minutes=2,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        settings = settings or {}
        cfg = ((settings.get("public_market_adapters") or {}).get(self.info.adapter_id) or {})
        timeout = int(cfg.get("timeout_seconds", 12))
        result = fetch_text(FUTURES_URL, timeout)
        observations: list[dict] = []
        if result["ok"]:
            try:
                observations = parse_whitebit_futures(
                    parse_json(result["text"]),
                    observed_at=result["received_at"],
                )
            except (TypeError, ValueError):
                observations = []
        if not observations:
            observations = [health_observation("WHITEBIT", FUTURES_URL, result, "whitebit_public_perpetuals")]
            return ScanBatch(
                source=self.info.source,
                candidates=[],
                observations=observations,
                metadata={"source_status": result["status"], "observation_count": len(observations), "depth_count": 0},
            )

        max_instruments = max(1, int(cfg.get("max_instruments", 120)))
        observations = observations[:max_instruments]
        depth_count = max(0, min(int(cfg.get("max_depth_books", 16)), len(observations)))
        funding_ranked = sorted(
            observations,
            key=lambda row: (abs(float(row.get("funding_bps") or 0.0)), float(row.get("quote_volume_24h") or 0.0)),
            reverse=True,
        )[:depth_count]
        enriched_by_id: dict[str, dict] = {}
        if funding_ranked:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(funding_ranked))) as pool:
                futures = {
                    pool.submit(_fetch_book, row, timeout, int(cfg.get("depth_levels", 50))): row["inst_id"]
                    for row in funding_ranked
                }
                for future in concurrent.futures.as_completed(futures):
                    try:
                        enriched_by_id[futures[future]] = future.result()
                    except Exception:  # noqa: BLE001 - one book must not stop the adapter.
                        continue
        observations = [enriched_by_id.get(row["inst_id"], row) for row in observations]
        candidates = [candidate for row in observations if (candidate := funding_candidate(row, settings))]
        return ScanBatch(
            source=self.info.source,
            candidates=candidates,
            observations=observations,
            metadata={
                "source_status": result["status"],
                "observation_count": len(observations),
                "depth_count": len(enriched_by_id),
                "candidate_count": len(candidates),
                "paper_only": True,
            },
        )


register_adapter(WhitebitPerpetualAdapter())
