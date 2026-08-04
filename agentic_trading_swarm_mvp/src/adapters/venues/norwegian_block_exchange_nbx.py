"""NBX public Nordic-fiat spot and tokenized-metals order books.

The adapter intentionally exposes observations only.  NBX's public order-book
endpoint does not grant account or order permissions, and no authenticated
surface is used here.
"""

from __future__ import annotations

import concurrent.futures
import re
import urllib.parse
from collections import defaultdict
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, parse_json, utc_now
from scan_batch import ScanBatch


API_ROOT = "https://api.nbx.com"
MARKETS_URL = API_ROOT + "/markets"
ORDER_BOOK_URL = API_ROOT + "/markets/{market}/orders"
DOCS_URL = "https://nbx.com/en/tpor"
METALS_URL = "https://nbx.com/blog-en/how-to-invest-in-digital-gold-and-silver-safely-in-uncertain-times"
MARKET_SURFACE = "nbx_public_nordic_fiat_order_book"
SUPPORTED_QUOTES = ("NOK", "SEK", "DKK", "EUR")

# NBX does not currently expose an unauthenticated market-catalog endpoint.
# This bounded set covers its advertised Nordic-fiat surface and both
# tokenized-metal products; operators may replace it through adapter settings.
DEFAULT_MARKETS = (
    "BTC-NOK",
    "ETH-NOK",
    "SOL-NOK",
    "USDC-NOK",
    "XRP-NOK",
    "ADA-NOK",
    "AVAX-NOK",
    "FGLD-NOK",
    "FSLVR-NOK",
    "BTC-SEK",
    "ETH-SEK",
    "BTC-DKK",
    "ETH-DKK",
    "BTC-EUR",
    "ETH-EUR",
    "FGLD-EUR",
    "FSLVR-EUR",
)


class NbxOrderBookParseError(ValueError):
    """Raised when a reachable NBX response no longer contains a usable book."""


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 < parsed < float("inf") else None


def _market_parts(market: str) -> tuple[str, str, str]:
    symbol = str(market or "").strip().upper().replace("/", "-")
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", symbol):
        raise NbxOrderBookParseError(f"invalid NBX market symbol: {market!r}")
    base, quote = symbol.split("-", 1)
    if quote not in SUPPORTED_QUOTES:
        raise NbxOrderBookParseError(f"unsupported NBX quote currency: {quote}")
    return symbol, base, quote


def market_order_book_url(market: str) -> str:
    symbol, _base, _quote = _market_parts(market)
    return ORDER_BOOK_URL.format(market=urllib.parse.quote(symbol, safe="-"))


def parse_nbx_markets(payload: object) -> list[str]:
    """Return active Nordic-fiat markets from NBX's public catalog."""

    if not isinstance(payload, list):
        raise NbxOrderBookParseError("NBX market-catalog response must be an array")
    markets: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("disabled") is True or item.get("cancelOnly") is True:
            continue
        if str(item.get("status") or "OK").upper() not in {"OK", "ACTIVE", "OPEN"}:
            continue
        try:
            symbol, _base, quote = _market_parts(str(item.get("id") or ""))
        except NbxOrderBookParseError:
            continue
        catalog_quote = str(item.get("quoteAsset") or quote).upper()
        if catalog_quote != quote:
            continue
        if symbol not in markets:
            markets.append(symbol)
    if not markets:
        raise NbxOrderBookParseError("NBX market catalog contained no active Nordic-fiat markets")
    return sorted(markets)


def parse_nbx_order_book(
    payload: object,
    *,
    market: str,
    received_at: str | None = None,
    source_url: str | None = None,
    max_levels: int = 50,
) -> dict:
    """Aggregate and normalize one official NBX anonymous order book."""

    symbol, base, quote = _market_parts(market)
    if not isinstance(payload, list):
        raise NbxOrderBookParseError("NBX order-book response must be an array")

    levels: dict[str, dict[float, float]] = {
        "BUY": defaultdict(float),
        "SELL": defaultdict(float),
    }
    invalid_rows = 0
    for item in payload:
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        side = str(item.get("side") or "").upper()
        price = _positive_float(item.get("price"))
        quantity = _positive_float(item.get("quantity"))
        if side not in levels or price is None or quantity is None:
            invalid_rows += 1
            continue
        levels[side][price] += quantity

    limit = max(1, min(int(max_levels), 250))
    bids = [[price, quantity] for price, quantity in sorted(levels["BUY"].items(), reverse=True)[:limit]]
    asks = [[price, quantity] for price, quantity in sorted(levels["SELL"].items())[:limit]]
    if not bids or not asks:
        detail = f"; {invalid_rows} invalid rows" if invalid_rows else ""
        raise NbxOrderBookParseError(f"NBX order book is not two-sided{detail}")

    bid = bids[0][0]
    ask = asks[0][0]
    if bid >= ask:
        raise NbxOrderBookParseError("NBX order book is locked or crossed")
    midpoint = (bid + ask) / 2.0
    spread = (ask - bid) / midpoint * 10_000.0
    bid_depth = sum(price * quantity for price, quantity in bids)
    ask_depth = sum(price * quantity for price, quantity in asks)
    total_depth = bid_depth + ask_depth
    timestamp = received_at or utc_now()
    is_metal = base in {"FGLD", "FSLVR"}
    url = source_url or market_order_book_url(symbol)

    return {
        "venue": "NBX",
        "inst_id": f"NBX:{symbol}",
        "instrument_id": f"NBX:{symbol}",
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "market_type": "spot",
        "market_surface": MARKET_SURFACE,
        "asset_class": "tokenized_precious_metal" if is_metal else "crypto_spot",
        "instrument_family": "tokenized_metal_spot" if is_metal else "crypto_spot",
        "trade_type": "official_market_reference",
        "direction": "watch_only",
        "last": midpoint,
        "price_type": "order_book_midpoint",
        "bid": bid,
        "ask": ask,
        "spread_bps": round(spread, 3),
        "book_levels": {"bids": bids, "asks": asks},
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "bid_depth_quote": round(bid_depth, 12),
        "ask_depth_quote": round(ask_depth, 12),
        "book_imbalance": round((bid_depth - ask_depth) / total_depth, 6) if total_depth else 0.0,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_order_book",
        "freshness_state": "fresh",
        "freshness_basis": "response_received",
        "freshness_age_seconds": 0.0,
        "session_status": "open_24_7",
        "observed_at": timestamp,
        "fetched_at": timestamp,
        "price_source": "NBX official public order book",
        "source_url": url,
        "documentation_url": DOCS_URL,
        "tokenized_metal_reference_url": METALS_URL if is_metal else None,
        "candidate_reject_reason": "public_order_book_watch_only_no_execution_route",
    }


def _failure_observation(
    market: str,
    result: dict[str, Any],
    *,
    parser_error: str | None = None,
) -> dict[str, Any]:
    symbol, base, quote = _market_parts(market)
    url = market_order_book_url(symbol)
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("NBX", url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"NBX:{symbol}:ADAPTER_HEALTH",
            "instrument_id": f"NBX:{symbol}:ADAPTER_HEALTH",
            "symbol": symbol,
            "base": base,
            "quote": quote,
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "documentation_url": DOCS_URL,
            "candidate_reject_reason": (
                "public_order_book_parser_failure"
                if parser_error
                else "public_order_book_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _configured_markets(
    cfg: dict[str, Any],
    discovered_markets: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    raw_markets = cfg.get("markets", discovered_markets or DEFAULT_MARKETS)
    raw_markets = raw_markets if isinstance(raw_markets, (list, tuple)) else DEFAULT_MARKETS
    markets: list[str] = []
    failures: list[dict[str, str]] = []
    for raw_market in raw_markets:
        try:
            symbol, _base, _quote = _market_parts(str(raw_market))
        except NbxOrderBookParseError as exc:
            failures.append({"market": str(raw_market), "source_url": API_ROOT, "error": str(exc)[:300]})
            continue
        if symbol not in markets:
            markets.append(symbol)
    if not markets:
        markets = list(DEFAULT_MARKETS)
    return markets[: max(1, min(int(cfg.get("max_markets", 60)), 100))], failures


class NorwegianBlockExchangeNbxAdapter:
    info = AdapterInfo(
        adapter_id="norwegian_block_exchange_nbx_public",
        venue="NBX",
        market_type="spot",
        source="NBX official public REST order books",
        capabilities=(
            "order_book",
            "best_bid_ask",
            "spread",
            "local_fiat",
            "tokenized_metals",
            "source_health",
        ),
        aliases=(
            "nbx",
            "norwegian block exchange",
            "nbx nordic fiat",
            "nbx tokenized gold silver",
        ),
        docs_url=DOCS_URL,
        runtime_entrypoint=(
            "adapters.venues.norwegian_block_exchange_nbx."
            "NorwegianBlockExchangeNbxAdapter"
        ),
        quote_assets=SUPPORTED_QUOTES,
        default_cache_minutes=2,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 12)))
        max_levels = max(1, min(int(cfg.get("depth_levels", 50)), 250))
        parser_failures: list[dict[str, str]] = []
        catalog_result: dict[str, Any] | None = None
        discovered_markets: list[str] | None = None
        if "markets" not in cfg:
            catalog_result = fetch_text(MARKETS_URL, timeout)
            if catalog_result.get("ok"):
                try:
                    discovered_markets = parse_nbx_markets(
                        parse_json(catalog_result.get("text") or "")
                    )
                except (NbxOrderBookParseError, TypeError, ValueError) as exc:
                    parser_failures.append(
                        {
                            "market": "catalog",
                            "source_url": MARKETS_URL,
                            "error": f"NBX market-catalog parser failed: {exc}"[:300],
                        }
                    )
        markets, configured_failures = _configured_markets(cfg, discovered_markets)
        parser_failures.extend(configured_failures)
        results: dict[str, dict[str, Any]] = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(int(cfg.get("workers", 6)), len(markets)))
        ) as pool:
            futures = {
                pool.submit(fetch_text, market_order_book_url(market), timeout): market
                for market in markets
            }
            for future in concurrent.futures.as_completed(futures):
                market = futures[future]
                try:
                    results[market] = future.result()
                except Exception as exc:  # noqa: BLE001 - retain other NBX books.
                    results[market] = {
                        "ok": False,
                        "status": "unavailable",
                        "http_status": None,
                        "error": str(exc)[:300],
                        "text": "",
                        "received_at": utc_now(),
                        "latency_ms": None,
                    }

        observations: list[dict] = []
        successful_markets: list[str] = []
        for market in markets:
            result = results[market]
            if not result.get("ok"):
                observations.append(_failure_observation(market, result))
                continue
            url = market_order_book_url(market)
            try:
                observation = parse_nbx_order_book(
                    parse_json(result.get("text") or ""),
                    market=market,
                    received_at=result.get("received_at"),
                    source_url=url,
                    max_levels=max_levels,
                )
            except (NbxOrderBookParseError, TypeError, ValueError) as exc:
                message = f"NBX {market} order-book parser failed: {exc}"[:300]
                parser_failures.append({"market": market, "source_url": url, "error": message})
                observations.append(_failure_observation(market, result, parser_error=message))
                continue
            observation["fetch_latency_ms"] = result.get("latency_ms")
            observation["http_status"] = result.get("http_status")
            observations.append(observation)
            successful_markets.append(market)

        statuses = [str(item.get("status") or "unavailable") for item in results.values()]
        catalog_failed = catalog_result is not None and not catalog_result.get("ok")
        if len(successful_markets) == len(markets) and not parser_failures and not catalog_failed:
            source_status = "reachable"
        elif successful_markets:
            source_status = "degraded"
        elif parser_failures:
            source_status = "degraded"
        elif statuses and all(status == "blocked" for status in statuses):
            source_status = "blocked"
        else:
            source_status = "unavailable"

        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
        fetch_status = {
            market: {
                "source_url": market_order_book_url(market),
                "fetch_status": str(results[market].get("status") or "unavailable"),
                "http_status": results[market].get("http_status"),
                "fetched_at": results[market].get("received_at"),
                "latency_ms": results[market].get("latency_ms"),
            }
            for market in markets
        }
        if catalog_result is not None:
            fetch_status["catalog"] = {
                "source_url": MARKETS_URL,
                "fetch_status": str(catalog_result.get("status") or "unavailable"),
                "http_status": catalog_result.get("http_status"),
                "fetched_at": catalog_result.get("received_at"),
                "latency_ms": catalog_result.get("latency_ms"),
            }
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 792,
                "source_status": source_status,
                "source_urls": [MARKETS_URL, *[market_order_book_url(market) for market in markets]],
                "documentation_urls": [DOCS_URL, METALS_URL],
                "fetch_status": fetch_status,
                "freshness_state": (
                    "fresh" if successful_markets else "unknown"
                ),
                "freshness_states": freshness_states,
                "session_state": session_states,
                "parser_failures": parser_failures,
                "configured_markets": markets,
                "market_discovery": "public_catalog" if discovered_markets else "configured_or_fallback",
                "successful_markets": successful_markets,
                "observation_count": len(observations),
                "real_observation_count": len(successful_markets),
                "candidate_count": 0,
                "paper_only": True,
            },
        )


register_adapter(NorwegianBlockExchangeNbxAdapter())
