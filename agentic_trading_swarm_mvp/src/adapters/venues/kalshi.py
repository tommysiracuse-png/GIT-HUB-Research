"""Kalshi anonymous prediction-market contracts and order books.

Only Kalshi's documented unauthenticated REST endpoints are used.  The adapter
does not expose candidate generation or any account/order surface; normalized
rows remain watch-only even when the public data is fresh.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import urllib.parse
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, parse_json, utc_now
from scan_batch import ScanBatch


API_ROOT = "https://external-api.kalshi.com/trade-api/v2"
MARKETS_ENDPOINT = API_ROOT + "/markets"
ORDER_BOOK_ENDPOINT = API_ROOT + "/markets/{ticker}/orderbook"
DOCS_URL = "https://docs.kalshi.com/getting_started/quick_start_market_data"
MARKETS_DOCS_URL = "https://docs.kalshi.com/api-reference/market/get-markets"
ORDER_BOOK_DOCS_URL = "https://docs.kalshi.com/getting_started/orderbook_responses"
MARKET_SURFACE = "kalshi_public_prediction_market_contracts"


class KalshiParseError(ValueError):
    """Raised when a reachable Kalshi response no longer matches its schema."""


def markets_url(limit: int = 100) -> str:
    query = urllib.parse.urlencode({"status": "open", "limit": max(1, min(int(limit), 1000))})
    return f"{MARKETS_ENDPOINT}?{query}"


def market_order_book_url(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    if not normalized or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in normalized):
        raise KalshiParseError(f"invalid Kalshi market ticker: {ticker!r}")
    return ORDER_BOOK_ENDPOINT.format(ticker=urllib.parse.quote(normalized, safe="-_."))


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _probability(value: Any) -> float | None:
    parsed = _float(value)
    return parsed if parsed is not None and 0.0 <= parsed <= 1.0 else None


def _nonnegative(value: Any) -> float | None:
    parsed = _float(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _session_status(item: dict[str, Any], received_at: str) -> str:
    status = str(item.get("status") or "open").strip().lower()
    if status in {"open", "active"}:
        close_at = _parse_time(item.get("close_time") or item.get("expiration_time"))
        observed = _parse_time(received_at)
        if close_at and observed and close_at <= observed:
            return "closed"
        return "open"
    if status in {"unopened", "pending"}:
        return "unopened"
    if status in {"closed", "settled", "finalized"}:
        return status
    return "unknown"


def parse_kalshi_markets(
    payload: object,
    *,
    received_at: str | None = None,
    source_url: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize one page of the official open-market catalog."""

    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise KalshiParseError("Kalshi markets response must contain a markets array")
    raw_markets = payload["markets"]
    timestamp = received_at or utc_now()
    url = source_url or markets_url()
    observations: list[dict[str, Any]] = []
    invalid_rows = 0
    for item in raw_markets:
        if not isinstance(item, dict):
            invalid_rows += 1
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        title = str(item.get("title") or "").strip()
        try:
            market_order_book_url(ticker)
        except KalshiParseError:
            invalid_rows += 1
            continue

        yes_bid = _probability(item.get("yes_bid_dollars"))
        yes_ask = _probability(item.get("yes_ask_dollars"))
        no_bid = _probability(item.get("no_bid_dollars"))
        no_ask = _probability(item.get("no_ask_dollars"))
        if yes_ask is None and no_bid is not None:
            yes_ask = 1.0 - no_bid
        if yes_bid is None and no_ask is not None:
            yes_bid = 1.0 - no_ask
        last_trade = _probability(item.get("last_price_dollars"))
        if yes_bid is not None and yes_ask is not None and yes_bid <= yes_ask:
            last = (yes_bid + yes_ask) / 2.0
            price_type = "yes_bid_ask_midpoint"
        elif last_trade is not None:
            last = last_trade
            price_type = "last_trade_probability"
        elif yes_bid is not None:
            last = yes_bid
            price_type = "yes_bid_probability"
        elif yes_ask is not None:
            last = yes_ask
            price_type = "yes_ask_probability"
        else:
            invalid_rows += 1
            continue

        spread = yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None
        market_updated_at = str(item.get("updated_time") or "") or None
        observations.append(
            {
                "venue": "KALSHI",
                "inst_id": f"KALSHI:{ticker}",
                "instrument_id": f"KALSHI:{ticker}",
                "symbol": ticker,
                "ticker": ticker,
                "event_ticker": str(item.get("event_ticker") or "") or None,
                "series_ticker": str(item.get("series_ticker") or "") or None,
                "title": title or ticker,
                "subtitle": str(item.get("subtitle") or item.get("yes_sub_title") or "") or None,
                "yes_sub_title": str(item.get("yes_sub_title") or "") or None,
                "no_sub_title": str(item.get("no_sub_title") or "") or None,
                "category": str(item.get("category") or "") or None,
                "base": ticker,
                "quote": "USD",
                "market_type": "prediction_market",
                "market_surface": MARKET_SURFACE,
                "asset_class": "prediction_market",
                "instrument_family": "binary_event_contract",
                "trade_type": "official_prediction_market_reference",
                "direction": "watch_only",
                "last": round(last, 8),
                "yes_probability": round(last, 8),
                "price_type": price_type,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "spread": round(spread, 8) if spread is not None and spread >= 0 else None,
                "spread_bps_of_payout": round(spread * 10_000.0, 3)
                if spread is not None and spread >= 0
                else None,
                "last_trade_probability": last_trade,
                "volume_contracts": _nonnegative(item.get("volume_fp")),
                "volume_24h_contracts": _nonnegative(item.get("volume_24h_fp")),
                "open_interest_contracts": _nonnegative(item.get("open_interest_fp")),
                "liquidity_dollars": _nonnegative(item.get("liquidity_dollars")),
                "open_time": str(item.get("open_time") or "") or None,
                "close_time": str(item.get("close_time") or "") or None,
                "expiration_time": str(item.get("expiration_time") or "") or None,
                "market_updated_at": market_updated_at,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_market_quote",
                "freshness_state": "fresh",
                "freshness_basis": "response_received",
                "freshness_age_seconds": 0.0,
                "session_status": _session_status(item, timestamp),
                "observed_at": timestamp,
                "fetched_at": timestamp,
                "price_source": "Kalshi official public market catalog",
                "source_url": url,
                "documentation_url": DOCS_URL,
                "candidate_reject_reason": "public_prediction_market_watch_only_no_execution_route",
            }
        )
    if raw_markets and not observations:
        raise KalshiParseError(
            f"Kalshi markets array contained no usable priced contracts; {invalid_rows} invalid rows"
        )
    observations.sort(
        key=lambda row: (
            float(row.get("volume_24h_contracts") or 0.0),
            float(row.get("liquidity_dollars") or 0.0),
        ),
        reverse=True,
    )
    return observations


def _book_levels(value: object, side: str, limit: int) -> tuple[list[list[float]], int]:
    if value is None:
        return [], 0
    if not isinstance(value, list):
        raise KalshiParseError(f"Kalshi orderbook {side}_dollars must be an array")
    levels: dict[float, float] = {}
    invalid = 0
    for level in value:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            invalid += 1
            continue
        price = _probability(level[0])
        quantity = _float(level[1])
        if price is None or quantity is None or quantity <= 0.0:
            invalid += 1
            continue
        levels[price] = levels.get(price, 0.0) + quantity
    return [[price, quantity] for price, quantity in sorted(levels.items(), reverse=True)[:limit]], invalid


def parse_kalshi_order_book(
    payload: object,
    *,
    market: dict[str, Any],
    received_at: str | None = None,
    source_url: str | None = None,
    max_levels: int = 50,
) -> dict[str, Any]:
    """Enrich a normalized contract with Kalshi's reciprocal YES/NO book."""

    if not isinstance(payload, dict) or not isinstance(payload.get("orderbook_fp"), dict):
        raise KalshiParseError("Kalshi orderbook response must contain an orderbook_fp object")
    body = payload["orderbook_fp"]
    if "yes_dollars" not in body or "no_dollars" not in body:
        raise KalshiParseError("Kalshi orderbook_fp must contain yes_dollars and no_dollars arrays")
    limit = max(1, min(int(max_levels), 1000))
    yes_bids, invalid_yes = _book_levels(body.get("yes_dollars"), "yes", limit)
    no_bids, invalid_no = _book_levels(body.get("no_dollars"), "no", limit)
    if not yes_bids and not no_bids and invalid_yes + invalid_no:
        raise KalshiParseError(
            f"Kalshi orderbook contained no usable levels; {invalid_yes + invalid_no} invalid levels"
        )

    book_yes_bid = yes_bids[0][0] if yes_bids else None
    book_no_bid = no_bids[0][0] if no_bids else None
    yes_bid = book_yes_bid if book_yes_bid is not None else _probability(market.get("yes_bid"))
    no_bid = book_no_bid if book_no_bid is not None else _probability(market.get("no_bid"))
    yes_ask = (
        round(1.0 - book_no_bid, 8)
        if book_no_bid is not None
        else _probability(market.get("yes_ask"))
    )
    no_ask = (
        round(1.0 - book_yes_bid, 8)
        if book_yes_bid is not None
        else _probability(market.get("no_ask"))
    )
    if book_yes_bid is not None and yes_ask is not None and book_no_bid is not None and book_yes_bid > yes_ask:
        raise KalshiParseError("Kalshi YES orderbook is crossed")
    if book_yes_bid is not None and book_no_bid is not None:
        last = (book_yes_bid + yes_ask) / 2.0
        price_type = "order_book_midpoint_probability"
    else:
        last = float(market["last"])
        price_type = str(market.get("price_type") or "market_probability")
    spread = yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None
    yes_depth = sum(quantity for _price, quantity in yes_bids)
    no_depth = sum(quantity for _price, quantity in no_bids)
    total_depth = yes_depth + no_depth
    book_state = "two_sided" if yes_bids and no_bids else "one_sided" if yes_bids or no_bids else "empty"
    timestamp = received_at or utc_now()
    ticker = str(market.get("ticker") or market.get("symbol") or "")
    url = source_url or market_order_book_url(ticker)
    return {
        **market,
        "last": round(last, 8),
        "yes_probability": round(last, 8),
        "price_type": price_type,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": round(spread, 8) if spread is not None else None,
        "spread_bps_of_payout": round(spread * 10_000.0, 3) if spread is not None else None,
        "book_levels": {"yes_bids": yes_bids, "no_bids": no_bids},
        "yes_bid_level_count": len(yes_bids),
        "no_bid_level_count": len(no_bids),
        "yes_depth_contracts": round(yes_depth, 8),
        "no_depth_contracts": round(no_depth, 8),
        "book_imbalance": round((yes_depth - no_depth) / total_depth, 6) if total_depth else 0.0,
        "quality_status": "official_order_book" if book_state != "empty" else "official_order_book_empty",
        "order_book_state": book_state,
        "freshness_state": "fresh",
        "freshness_basis": "response_received",
        "freshness_age_seconds": 0.0,
        "observed_at": timestamp,
        "fetched_at": timestamp,
        "price_source": (
            "Kalshi official public order book"
            if book_state == "two_sided"
            else market.get("price_source") or "Kalshi official public market catalog"
        ),
        "contract_source_url": market.get("source_url"),
        "source_url": url if book_state == "two_sided" else market.get("source_url") or url,
        "order_book_source_url": url,
        "order_book_fetched_at": timestamp,
    }


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _fetch_evidence(url: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
    }


class KalshiPublicPredictionMarketsAdapter:
    info = AdapterInfo(
        adapter_id="kalshi_public_prediction_markets",
        venue="KALSHI",
        market_type="prediction_market",
        source="Kalshi official anonymous prediction-market REST API",
        capabilities=(
            "market_catalog",
            "ticker",
            "order_book",
            "best_bid_ask",
            "spread",
            "volume",
            "open_interest",
            "source_health",
        ),
        aliases=("kalshi", "kalshi prediction markets", "kalshi event contracts"),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.kalshi.KalshiPublicPredictionMarketsAdapter",
        quote_assets=("USD",),
        default_cache_minutes=1,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 12)))
        market_limit = max(1, min(int(cfg.get("market_limit", 100)), 1000))
        max_books = max(0, min(int(cfg.get("max_order_books", 20)), market_limit))
        depth_levels = max(1, min(int(cfg.get("depth_levels", 50)), 1000))
        catalog_url = markets_url(market_limit)
        catalog_result = fetch_text(catalog_url, timeout)
        fetch_status: dict[str, dict[str, Any]] = {
            "catalog": _fetch_evidence(catalog_url, catalog_result)
        }
        parser_failures: list[dict[str, str]] = []

        if not catalog_result.get("ok"):
            observation = health_observation("KALSHI", catalog_url, catalog_result, MARKET_SURFACE)
            observation.update(
                {
                    "fetch_status": str(catalog_result.get("status") or "unavailable"),
                    "freshness_state": "unknown",
                    "freshness_basis": "unavailable",
                    "freshness_age_seconds": None,
                    "documentation_url": DOCS_URL,
                    "candidate_reject_reason": "public_prediction_market_source_unavailable",
                }
            )
            return self._batch(
                [observation],
                str(catalog_result.get("status") or "unavailable"),
                fetch_status,
                parser_failures,
                catalog_url,
                [],
            )

        try:
            observations = parse_kalshi_markets(
                parse_json(catalog_result.get("text") or ""),
                received_at=catalog_result.get("received_at"),
                source_url=catalog_url,
            )
        except (KalshiParseError, TypeError, ValueError) as exc:
            message = f"Kalshi market-catalog parser failed: {exc}"[:300]
            parser_failures.append({"source_url": catalog_url, "error": message})
            evidence = {**catalog_result, "status": "degraded", "error": message}
            observation = health_observation("KALSHI", catalog_url, evidence, MARKET_SURFACE)
            observation.update(
                {
                    "fetch_status": str(catalog_result.get("status") or "reachable"),
                    "freshness_state": "unknown",
                    "freshness_basis": "parser_failure",
                    "freshness_age_seconds": None,
                    "parser_failure": message,
                    "documentation_url": DOCS_URL,
                    "candidate_reject_reason": "public_prediction_market_parser_failure",
                }
            )
            return self._batch(
                [observation], "degraded", fetch_status, parser_failures, catalog_url, []
            )

        selected = observations[: min(max_books, len(observations))]
        book_results: dict[str, dict[str, Any]] = {}
        if selected:
            workers = max(1, min(int(cfg.get("workers", 6)), len(selected)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(fetch_text, market_order_book_url(row["ticker"]), timeout): row["ticker"]
                    for row in selected
                }
                for future in concurrent.futures.as_completed(futures):
                    ticker = futures[future]
                    try:
                        book_results[ticker] = future.result()
                    except Exception as exc:  # noqa: BLE001 - retain the other public books.
                        book_results[ticker] = {
                            "ok": False,
                            "status": "unavailable",
                            "http_status": None,
                            "error": str(exc)[:300],
                            "text": "",
                            "received_at": utc_now(),
                            "latency_ms": None,
                        }

        enriched_by_ticker: dict[str, dict[str, Any]] = {}
        successful_books: list[str] = []
        for row in selected:
            ticker = row["ticker"]
            url = market_order_book_url(ticker)
            result = book_results[ticker]
            fetch_status[ticker] = _fetch_evidence(url, result)
            if not result.get("ok"):
                enriched_by_ticker[ticker] = {
                    **row,
                    "order_book_fetch_status": str(result.get("status") or "unavailable"),
                    "order_book_source_url": url,
                    "order_book_error": str(result.get("error") or "Public order book unavailable.")[:300],
                    "candidate_reject_reason": "public_order_book_source_unavailable",
                }
                continue
            try:
                enriched = parse_kalshi_order_book(
                    parse_json(result.get("text") or ""),
                    market=row,
                    received_at=result.get("received_at"),
                    source_url=url,
                    max_levels=depth_levels,
                )
            except (KalshiParseError, TypeError, ValueError) as exc:
                message = f"Kalshi {ticker} order-book parser failed: {exc}"[:300]
                parser_failures.append({"market": ticker, "source_url": url, "error": message})
                enriched_by_ticker[ticker] = {
                    **row,
                    "order_book_fetch_status": str(result.get("status") or "reachable"),
                    "order_book_source_url": url,
                    "parser_failure": message,
                    "candidate_reject_reason": "public_order_book_parser_failure",
                }
                continue
            enriched["order_book_fetch_status"] = "reachable"
            enriched["fetch_latency_ms"] = result.get("latency_ms")
            enriched["http_status"] = result.get("http_status")
            enriched_by_ticker[ticker] = enriched
            successful_books.append(ticker)

        observations = [enriched_by_ticker.get(row["ticker"], row) for row in observations]
        book_statuses = [str(result.get("status") or "unavailable") for result in book_results.values()]
        if parser_failures or (book_statuses and any(status != "reachable" for status in book_statuses)):
            source_status = "degraded"
        else:
            source_status = "reachable"
        return self._batch(
            observations,
            source_status,
            fetch_status,
            parser_failures,
            catalog_url,
            successful_books,
        )

    def _batch(
        self,
        observations: list[dict[str, Any]],
        source_status: str,
        fetch_status: dict[str, dict[str, Any]],
        parser_failures: list[dict[str, str]],
        catalog_url: str,
        successful_books: list[str],
    ) -> ScanBatch:
        real_count = sum(
            1 for row in observations if row.get("data_status") == "reachable" and row.get("last") is not None
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1160,
                "source_status": source_status,
                "source_urls": [entry["source_url"] for entry in fetch_status.values()],
                "documentation_urls": [DOCS_URL, MARKETS_DOCS_URL, ORDER_BOOK_DOCS_URL],
                "fetch_status": fetch_status,
                "freshness_state": "fresh" if real_count else "unknown",
                "freshness_states": sorted(
                    {str(row.get("freshness_state") or "unknown") for row in observations}
                ),
                "session_state": sorted(
                    {str(row.get("session_status") or "unknown") for row in observations}
                ),
                "parser_failures": parser_failures,
                "catalog_source_url": catalog_url,
                "successful_order_books": successful_books,
                "observation_count": len(observations),
                "real_observation_count": real_count,
                "order_book_count": len(successful_books),
                "candidate_count": 0,
                "paper_only": True,
            },
        )


register_adapter(KalshiPublicPredictionMarketsAdapter())
