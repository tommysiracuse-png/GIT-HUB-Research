"""Targeted public order-book enrichment for frontier crypto observations."""

from __future__ import annotations

import collections
import concurrent.futures
import datetime as dt
import json
import math
import re
import sqlite3
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


QUALITY_NOTIONALS = (100.0, 250.0, 1000.0)
CRITICAL_ANOMALIES = {
    "crossed_book",
    "locked_book",
    "empty_book",
    "one_sided_book",
    "invalid_best_prices",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _timestamp_to_iso(value: object, unit: str = "auto") -> str | None:
    if isinstance(value, str):
        parsed = _parse_iso(value)
        if parsed is not None:
            return parsed.isoformat()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if unit == "milliseconds" or (unit == "auto" and numeric > 10_000_000_000):
        numeric /= 1000.0
    try:
        return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_json(url: str, timeout: int) -> dict:
    started = time.perf_counter()
    received_at = _utc_now()
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 inefficiency-radar/0.2",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            received_at = _utc_now()
            return {
                "ok": True,
                "status": "reachable",
                "http_status": str(response.status),
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "received_at": received_at,
                "payload": payload,
            }
    except Exception as exc:  # noqa: BLE001
        status = "blocked" if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403, 451} else "unavailable"
        return {
            "ok": False,
            "status": status,
            "http_status": str(exc)[:300],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "received_at": received_at,
            "payload": None,
        }


def _normalize_bitso_symbol(symbol: str) -> str:
    value = re.sub(r"\s+", "", str(symbol or "")).strip().lower()
    if not value:
        return value
    value = value.replace("-", "_").replace("/", "_").replace(":", "_")
    if "_" in value:
        parts = [part for part in value.split("_") if part]
        while parts and parts[-1] in {"spot", "book", "orderbook"}:
            parts = parts[:-1]
        while len(parts) >= 3 and parts[-1] == parts[-2]:
            parts = parts[:-1]
        return "_".join(parts)
    if len(value) > 3 and value.endswith("mxn"):
        return f"{value[:-3]}_mxn"
    return value


def _format_symbol(venue: str, symbol: str) -> str:
    if venue in {"BYBIT", "BYBIT_SPOT"}:
        compact = re.sub(r"[^A-Za-z0-9]", "", symbol or "")
        return compact.upper() or str(symbol).upper()
    if venue == "BITGET":
        return symbol.replace("_SPBL", "")
    if venue == "BITSO":
        # Bitso depth endpoints expect lowercase book ids like ``btc_mxn``.
        # Frontier observations may carry slash, dash, or compact MXN pairs
        # after venue-map normalization, so normalize them here before URL
        # construction to keep paper-only depth enrichment active for MXN books.
        return _normalize_bitso_symbol(symbol)
    if venue in {"INDODAX", "QUIDAX", "BUDA"}:
        return symbol.lower()
    if venue == "VALR":
        return symbol.replace("-", "").replace("_", "").replace("/", "").upper()
    if venue == "BITKUB":
        parts = [part for part in symbol.lower().split("_") if part]
        if len(parts) == 2:
            return f"{parts[1]}_{parts[0]}"
    return symbol


def _build_depth_url(observation: dict, depth_config: dict, levels: int) -> str:
    symbol = urllib.parse.quote(_format_symbol(str(observation["venue"]), str(observation["symbol"])), safe="-_")
    limit = min(int(depth_config.get("max_levels", levels)), levels)
    return str(depth_config["url_template"]).format(symbol=symbol, limit=limit)


def _extract_depth(parser: str, payload: object, received_at: str) -> dict:
    data = payload or {}
    bids: list = []
    asks: list = []
    book_timestamp = None
    freshness_basis = "response_received"
    if parser in {"binance_depth", "mexc_depth", "coinbase_book"}:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
    elif parser == "kucoin_level2":
        body = data.get("data") or {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("time") or body.get("timestamp"))
    elif parser == "gate_order_book":
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        book_timestamp = _timestamp_to_iso(data.get("current") or data.get("update"))
    elif parser in {"bitget_orderbook", "bybit_orderbook"}:
        body = data.get("data") or {}
        if parser == "bybit_orderbook":
            body = data.get("result") or body
        bids = body.get("bids") or body.get("b") or []
        asks = body.get("asks") or body.get("a") or []
        book_timestamp = _timestamp_to_iso(body.get("ts") or body.get("cts"))
    elif parser == "kraken_depth":
        values = list((data.get("result") or {}).values())
        body = values[0] if values else {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        timestamps = [
            _as_float(row[2])
            for row in [*bids[:1], *asks[:1]]
            if isinstance(row, (list, tuple)) and len(row) > 2
        ]
        timestamps = [value for value in timestamps if value is not None]
        if timestamps:
            book_timestamp = _timestamp_to_iso(max(timestamps), unit="seconds")
    elif parser == "okx_books":
        rows = data.get("data") or []
        body = rows[0] if rows else {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("ts"))
    elif parser in {"luno_orderbook", "valr_orderbook"}:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        book_timestamp = _timestamp_to_iso(data.get("timestamp"))
    elif parser == "quidax_depth":
        body = data.get("data") or data
        bids = body.get("bids") or body.get("buy") or []
        asks = body.get("asks") or body.get("sell") or []
    elif parser == "indodax_depth":
        bids = data.get("buy") or data.get("bids") or []
        asks = data.get("sell") or data.get("asks") or []
    elif parser == "bitkub_depth":
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        bids = body.get("bids") or body.get("bid") or []
        asks = body.get("asks") or body.get("ask") or []
    elif parser == "bitso_order_book":
        body = data.get("payload") or data
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("updated_at")) or body.get("updated_at")
    elif parser in {"mercado_bitcoin_orderbook", "buda_order_book"}:
        body = data.get("order_book") if parser == "buda_order_book" else data
        body = body or {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
    else:
        raise ValueError(f"Unsupported depth parser: {parser}")
    if book_timestamp:
        freshness_basis = "exchange_timestamp"
    else:
        book_timestamp = received_at
    return {
        "bids": bids,
        "asks": asks,
        "book_timestamp": book_timestamp,
        "freshness_basis": freshness_basis,
    }


def _should_prefer_trailing_price_quantity(
    first_price: float | None,
    first_quantity: float | None,
    trailing_price: float | None,
    trailing_quantity: float | None,
) -> bool:
    if trailing_price is None or trailing_quantity is None:
        return False
    if trailing_price <= 0 or trailing_quantity <= 0:
        return False
    if first_price is None or first_quantity is None:
        return True
    if first_price <= 0 or first_quantity <= 0:
        return True
    if first_price >= 1_000_000_000:
        return True
    return first_price < first_quantity


def _normalize_levels(raw_levels: list, side: str, max_levels: int) -> tuple[list[list[float]], list[str]]:
    valid: list[list[float]] = []
    anomalies: list[str] = []
    seen_prices: set[float] = set()
    original_prices: list[float] = []
    for raw in raw_levels[:max_levels]:
        if isinstance(raw, dict):
            price = _as_float(
                raw.get("price")
                or raw.get("rate")
                or raw.get("p")
                or raw.get("bid")
                or raw.get("ask")
            )
            quantity = _as_float(
                raw.get("volume")
                or raw.get("quantity")
                or raw.get("qty")
                or raw.get("amount")
                or raw.get("baseAmount")
            )
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            # Some venues include id/timestamp before quantity/price. If the
            # first two values do not look like price/quantity, the venue parser
            # should still leave the last two usable values in order.
            price = _as_float(raw[0])
            quantity = _as_float(raw[1])
            if len(raw) >= 4:
                alt_quantity = _as_float(raw[-2])
                alt_price = _as_float(raw[-1])
                if _should_prefer_trailing_price_quantity(price, quantity, alt_price, alt_quantity):
                    price = alt_price
                    quantity = alt_quantity
        else:
            anomalies.append("invalid_level_shape")
            continue
        if price is None or quantity is None or price <= 0 or quantity <= 0:
            anomalies.append("invalid_level_value")
            continue
        original_prices.append(price)
        if price in seen_prices:
            anomalies.append("duplicate_price_level")
            continue
        seen_prices.add(price)
        valid.append([price, quantity])
    expected = sorted(original_prices, reverse=side == "bids")
    if original_prices and original_prices != expected:
        anomalies.append("unsorted_levels")
    valid.sort(key=lambda row: row[0], reverse=side == "bids")
    return valid, sorted(set(anomalies))


def _depth_within(levels: list[list[float]], mid: float, band_bps: float, side: str) -> float:
    if mid <= 0:
        return 0.0
    if side == "bids":
        threshold = mid * (1.0 - band_bps / 10_000.0)
        chosen = [row for row in levels if row[0] >= threshold]
    else:
        threshold = mid * (1.0 + band_bps / 10_000.0)
        chosen = [row for row in levels if row[0] <= threshold]
    return sum(price * quantity for price, quantity in chosen)


def simulate_fill(levels: list[list[float]], side: str, notional_usd: float) -> dict:
    if not levels or notional_usd <= 0:
        return {"filled": False, "average_price": None, "slippage_bps": None, "depth_used_usd": 0.0}
    best = levels[0][0]
    remaining_quote = float(notional_usd)
    base_filled = 0.0
    quote_filled = 0.0
    for price, quantity in levels:
        level_quote = price * quantity
        used_quote = min(remaining_quote, level_quote)
        base_filled += used_quote / price
        quote_filled += used_quote
        remaining_quote -= used_quote
        if remaining_quote <= 1e-9:
            break
    if remaining_quote > max(0.01, notional_usd * 0.001) or base_filled <= 0:
        return {
            "filled": False,
            "average_price": None,
            "slippage_bps": None,
            "depth_used_usd": round(quote_filled, 3),
        }
    average = quote_filled / base_filled
    if side == "buy":
        slippage = (average / best - 1.0) * 10_000.0
    else:
        slippage = (1.0 - average / best) * 10_000.0
    return {
        "filled": True,
        "average_price": round(average, 12),
        "slippage_bps": round(max(0.0, slippage), 3),
        "depth_used_usd": round(quote_filled, 3),
    }


def _quality_score(
    observation: dict,
    depth: dict,
    freshness_age_seconds: float,
    freshness_basis: str,
) -> tuple[float, dict]:
    bid_depth = float(depth["depth_usd"]["bid"]["10"] or 0.0)
    ask_depth = float(depth["depth_usd"]["ask"]["10"] or 0.0)
    depth_component = min(1.0, min(bid_depth, ask_depth) / 1000.0) * 30.0
    buy_slip = depth["fills"]["buy"]["1000"].get("slippage_bps")
    sell_slip = depth["fills"]["sell"]["1000"].get("slippage_bps")
    worst_slip = max(
        [value for value in (buy_slip, sell_slip) if value is not None],
        default=100.0,
    )
    slippage_component = max(0.0, 1.0 - worst_slip / 50.0) * 25.0
    spread = float(observation.get("spread_bps") or 999.0)
    spread_component = max(0.0, 1.0 - spread / 20.0) * 20.0
    freshness_component = max(0.0, 1.0 - freshness_age_seconds / 90.0) * 15.0
    if freshness_basis == "response_received":
        freshness_component = min(freshness_component, 10.0)
    volume = float(observation.get("quote_volume_24h") or 0.0)
    depth_25 = min(
        float(depth["depth_usd"]["bid"]["25"] or 0.0),
        float(depth["depth_usd"]["ask"]["25"] or 0.0),
    )
    if volume >= 25_000 and depth_25 >= 1000:
        volume_component = 10.0
    elif volume >= 25_000 and depth_25 >= 250:
        volume_component = 7.0
    elif volume > 0 and depth_25 > 0:
        volume_component = 3.0
    else:
        volume_component = 0.0
    components = {
        "executable_depth": round(depth_component, 3),
        "simulated_slippage": round(slippage_component, 3),
        "spread": round(spread_component, 3),
        "freshness": round(freshness_component, 3),
        "volume_credibility": round(volume_component, 3),
    }
    return round(sum(components.values()), 3), components


def analyze_book(
    observation: dict,
    raw_book: dict,
    *,
    latency_ms: float,
    received_at: str,
    max_levels: int = 50,
    baseline_latency_ms: float | None = None,
    fresh_seconds: float = 30.0,
) -> dict:
    bids, bid_anomalies = _normalize_levels(raw_book.get("bids") or [], "bids", max_levels)
    asks, ask_anomalies = _normalize_levels(raw_book.get("asks") or [], "asks", max_levels)
    anomalies = [*bid_anomalies, *ask_anomalies]
    if not bids and not asks:
        anomalies.append("empty_book")
    elif not bids or not asks:
        anomalies.append("one_sided_book")
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        anomalies.append("invalid_best_prices")
        mid = float(observation.get("last") or 0.0)
    else:
        mid = (best_bid + best_ask) / 2.0
        if best_bid > best_ask:
            anomalies.append("crossed_book")
        elif best_bid == best_ask:
            anomalies.append("locked_book")
    ticker_mid = (
        (float(observation.get("bid")) + float(observation.get("ask"))) / 2.0
        if observation.get("bid") and observation.get("ask")
        else float(observation.get("last") or 0.0)
    )
    midpoint_mismatch_bps = (
        abs(mid / ticker_mid - 1.0) * 10_000.0 if mid > 0 and ticker_mid > 0 else None
    )
    if midpoint_mismatch_bps is not None and midpoint_mismatch_bps > 20.0:
        anomalies.append("ticker_book_midpoint_mismatch")
    timestamp = raw_book.get("book_timestamp") or received_at
    parsed_timestamp = _parse_iso(timestamp)
    parsed_received = _parse_iso(received_at) or dt.datetime.now(dt.timezone.utc)
    freshness_age = max(0.0, (parsed_received - parsed_timestamp).total_seconds()) if parsed_timestamp else 0.0
    if freshness_age > fresh_seconds:
        anomalies.append("stale_book")
    if latency_ms > 2000.0:
        anomalies.append("high_latency")
    if baseline_latency_ms and latency_ms > max(2000.0, baseline_latency_ms * 3.0):
        anomalies.append("latency_outlier")

    depth_usd = {
        side: {
            str(band): round(_depth_within(levels, mid, float(band), side), 3)
            for band in (5, 10, 25)
        }
        for side, levels in (("bid", bids), ("ask", asks))
    }
    fills = {"buy": {}, "sell": {}}
    for notional in QUALITY_NOTIONALS:
        fills["buy"][str(int(notional))] = simulate_fill(asks, "buy", notional)
        fills["sell"][str(int(notional))] = simulate_fill(bids, "sell", notional)
    if not fills["buy"]["1000"]["filled"] or not fills["sell"]["1000"]["filled"]:
        anomalies.append("depth_cliff")
    total_10 = depth_usd["bid"]["10"] + depth_usd["ask"]["10"]
    imbalance = (
        (depth_usd["bid"]["10"] - depth_usd["ask"]["10"]) / total_10
        if total_10 > 0
        else 0.0
    )
    all_25 = [
        price * quantity
        for levels, side in ((bids, "bids"), (asks, "asks"))
        for price, quantity in levels
        if (
            (side == "bids" and price >= mid * (1.0 - 25.0 / 10_000.0))
            or (side == "asks" and price <= mid * (1.0 + 25.0 / 10_000.0))
        )
    ]
    concentration = max(all_25) / sum(all_25) if all_25 and sum(all_25) > 0 else 1.0
    if float(observation.get("quote_volume_24h") or 0.0) >= 1_000_000 and min(
        depth_usd["bid"]["25"], depth_usd["ask"]["25"]
    ) < 100.0:
        anomalies.append("reported_volume_depth_mismatch")

    depth = {
        "bids": bids,
        "asks": asks,
        "depth_usd": depth_usd,
        "fills": fills,
        "imbalance_10bps": round(imbalance, 4),
        "depth_concentration_25bps": round(concentration, 4),
    }
    score, components = _quality_score(
        observation,
        depth,
        freshness_age,
        str(raw_book.get("freshness_basis") or "response_received"),
    )
    anomaly_flags = sorted(set(anomalies))
    critical = sorted(CRITICAL_ANOMALIES.intersection(anomaly_flags))
    status = "verified" if not anomaly_flags else "degraded"
    return {
        "quality_status": status,
        "quality_score": score,
        "quality_components": components,
        "book_timestamp": timestamp,
        "book_observed_at": received_at,
        "freshness_basis": raw_book.get("freshness_basis") or "response_received",
        "freshness_age_seconds": round(freshness_age, 3),
        "depth_latency_ms": round(latency_ms, 3),
        "book_mid": round(mid, 12) if mid else None,
        "ticker_book_midpoint_mismatch_bps": (
            round(midpoint_mismatch_bps, 3) if midpoint_mismatch_bps is not None else None
        ),
        "book_levels": {"bids": bids, "asks": asks},
        "depth_usd": depth_usd,
        "simulated_fills": fills,
        "book_imbalance_10bps": depth["imbalance_10bps"],
        "depth_concentration_25bps": depth["depth_concentration_25bps"],
        "anomaly_flags": anomaly_flags,
        "critical_anomaly_flags": critical,
    }


def _unknown_quality(observation: dict, result: dict | None, reason: str) -> dict:
    status = (result or {}).get("status") or "unknown"
    return {
        "quality_status": "blocked" if status == "blocked" else "unknown",
        "quality_score": None,
        "quality_components": {},
        "book_timestamp": None,
        "book_observed_at": (result or {}).get("received_at") or _utc_now(),
        "freshness_basis": "unavailable",
        "freshness_age_seconds": None,
        "depth_latency_ms": (result or {}).get("latency_ms"),
        "book_mid": None,
        "ticker_book_midpoint_mismatch_bps": None,
        "book_levels": {"bids": [], "asks": []},
        "depth_usd": {"bid": {"5": 0.0, "10": 0.0, "25": 0.0}, "ask": {"5": 0.0, "10": 0.0, "25": 0.0}},
        "simulated_fills": {
            side: {
                str(int(notional)): {
                    "filled": False,
                    "average_price": None,
                    "slippage_bps": None,
                    "depth_used_usd": 0.0,
                }
                for notional in QUALITY_NOTIONALS
            }
            for side in ("buy", "sell")
        },
        "book_imbalance_10bps": None,
        "depth_concentration_25bps": None,
        "anomaly_flags": [reason],
        "critical_anomaly_flags": [],
        "depth_http_status": (result or {}).get("http_status"),
    }


def _venue_depth_targets(registry: dict) -> dict[str, dict]:
    return {
        str(row["venue"]): row
        for row in registry.get("venues", [])
        if row.get("enabled", True) and isinstance(row.get("depth"), dict)
    }


def _snapshot_rotation_state(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        select inst_id, observed_at, quality_status
        from frontier_quality_snapshots
        order by inst_id asc, observed_at desc
        """
    ).fetchall()
    state: dict[str, dict] = {}
    for row in rows:
        inst_id = str(row["inst_id"])
        item = state.setdefault(
            inst_id,
            {
                "last_observed_at": row["observed_at"],
                "consecutive_verified_count": 0,
                "counting": True,
            },
        )
        if item["counting"] and row["quality_status"] == "verified":
            item["consecutive_verified_count"] += 1
        else:
            item["counting"] = False
    return state


def _snapshot_sort_key(row: dict, state: dict[str, dict]) -> tuple:
    inst_id = str(row.get("instrument_id"))
    item = state.get(inst_id, {})
    verified_count = int(item.get("consecutive_verified_count") or 0)
    last_seen = item.get("last_observed_at") or ""
    return (verified_count, last_seen, -float(row.get("quote_volume_24h") or 0.0))


def _known_quality_rate_from_state(observations: list[dict], state: dict[str, dict]) -> float:
    reachable = [
        row
        for row in observations
        if row.get("instrument_id") and row.get("data_status") == "reachable"
    ]
    if not reachable:
        return 0.0
    known = sum(
        1
        for row in reachable
        if int((state.get(str(row.get("instrument_id"))) or {}).get("consecutive_verified_count") or 0) > 0
    )
    return known / len(reachable)


def _quality_target_escalation(
    observations: list[dict],
    snapshot_state: dict[str, dict],
    cfg: dict,
    base_max_total: int,
    base_max_per_venue: int,
) -> dict:
    target = float(cfg.get("known_quality_rate_target", 0.25))
    reachable_count = sum(1 for row in observations if row.get("instrument_id") and row.get("data_status") == "reachable")
    historical = _known_quality_rate_from_state(observations, snapshot_state)
    current_cycle = min(1.0, base_max_total / reachable_count) if reachable_count else 0.0
    current = min(historical, current_cycle)
    enabled = bool(cfg.get("quality_target_escalation_enabled", False))
    if not enabled or not observations or current >= target:
        return {
            "enabled": enabled,
            "active": False,
            "known_quality_rate_before_selection": round(current, 4),
            "historical_known_quality_rate": round(historical, 4),
            "current_cycle_quality_rate_at_base_budget": round(current_cycle, 4),
            "known_quality_rate_target": round(target, 4),
            "max_symbols_per_cycle": base_max_total,
            "max_symbols_per_venue": base_max_per_venue,
            "extra_symbols_requested": 0,
            "starved_venue_reserve": 0,
        }
    extra = int(cfg.get("quality_target_extra_symbols_per_cycle", 0) or 0)
    max_total = min(int(cfg.get("quality_target_max_symbols_per_cycle", base_max_total + extra)), base_max_total + extra)
    max_per_venue = max(
        base_max_per_venue,
        min(
            int(cfg.get("quality_target_max_symbols_per_venue", base_max_per_venue)),
            base_max_per_venue + max(1, min(extra, extra // 6 if extra >= 6 else extra)),
        ),
    )
    return {
        "enabled": enabled,
        "active": True,
        "known_quality_rate_before_selection": round(current, 4),
        "historical_known_quality_rate": round(historical, 4),
        "current_cycle_quality_rate_at_base_budget": round(current_cycle, 4),
        "known_quality_rate_target": round(target, 4),
        "max_symbols_per_cycle": max_total,
        "max_symbols_per_venue": max_per_venue,
        "extra_symbols_requested": max(0, max_total - base_max_total),
        "starved_venue_reserve": int(cfg.get("quality_target_starved_venue_reserve_per_cycle", 0) or 0),
    }


def _starved_sort_key(row: dict, state: dict[str, dict], starved_venues: set[str]) -> tuple:
    venue = str(row.get("venue") or "").upper()
    return (0 if venue in starved_venues else 1, *_snapshot_sort_key(row, state))


def _venue_depth_minimum_targets(cfg: dict, starved_venues: set[str]) -> dict[str, int]:
    fallback = max(0, int(cfg.get("starved_venue_min_depth_per_cycle", 0) or 0))
    configured = cfg.get("venue_depth_minimums", {}) or {}
    targets = {venue: fallback for venue in starved_venues if fallback > 0}
    for venue, value in configured.items():
        target = max(0, int(value or 0))
        if target > 0:
            targets[str(venue).upper()] = target
    return targets


def _venue_count(counts: collections.Counter[str], venue: str) -> int:
    venue_upper = str(venue).upper()
    return sum(count for key, count in counts.items() if str(key).upper() == venue_upper)


def _selection_quota_report(
    observations: list[dict],
    selected: list[dict],
    snapshot_state: dict[str, dict],
    targets: dict[str, int],
    max_total: int,
    max_per_venue: int,
) -> dict[str, dict]:
    observed: collections.Counter[str] = collections.Counter()
    reachable: collections.Counter[str] = collections.Counter()
    selected_counts: collections.Counter[str] = collections.Counter()
    known_prior: collections.Counter[str] = collections.Counter()
    for row in observations:
        venue = str(row.get("venue") or "unknown").upper()
        if venue not in targets:
            continue
        if row.get("instrument_id"):
            observed[venue] += 1
        if row.get("instrument_id") and row.get("data_status") == "reachable":
            reachable[venue] += 1
            state = snapshot_state.get(str(row.get("instrument_id"))) or {}
            if int(state.get("consecutive_verified_count") or 0) > 0:
                known_prior[venue] += 1
    for row in selected:
        venue = str(row.get("venue") or "unknown").upper()
        if venue in targets:
            selected_counts[venue] += 1

    report: dict[str, dict] = {}
    total_selected = len(selected)
    for venue, target in sorted(targets.items()):
        observed_count = int(observed.get(venue, 0))
        reachable_count = int(reachable.get(venue, 0))
        selected_count = int(selected_counts.get(venue, 0))
        required = min(target, reachable_count, max_per_venue)
        if observed_count == 0:
            status = "missed"
            missed_reason = "no_observations"
        elif reachable_count == 0:
            status = "missed"
            missed_reason = "no_reachable_observations"
        elif selected_count >= required:
            status = "met"
            missed_reason = None
        elif observed_count < target or reachable_count < target:
            status = "partial"
            missed_reason = "insufficient_reachable_observations"
        elif selected_count >= max_per_venue:
            status = "partial"
            missed_reason = "per_venue_cap"
        elif total_selected >= max_total:
            status = "partial"
            missed_reason = "total_cycle_cap"
        elif known_prior.get(venue, 0) >= reachable_count:
            status = "partial"
            missed_reason = "already_verified_or_lower_priority"
        else:
            status = "partial"
            missed_reason = "unfilled_after_priority_selection"
        report[venue] = {
            "target_selected_this_cycle": int(target),
            "target_after_caps": int(required),
            "observed_count": observed_count,
            "reachable_count": reachable_count,
            "selected_this_cycle": selected_count,
            "previously_verified_reachable_count": int(known_prior.get(venue, 0)),
            "status": status,
            "missed_reason": missed_reason,
        }
    return report


def _exploit_more_market_keys(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            """
            select market_key
            from market_hunter_directives
            where status = 'open' and directive = 'exploit_more'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["market_key"]) for row in rows if row["market_key"]}


def _open_growth_market_keys(conn: sqlite3.Connection) -> set[str]:
    keys: set[str] = set()
    try:
        rows = conn.execute(
            """
            select signal_key
            from growth_experiments
            where status = 'open' and priority >= 70
            """
        ).fetchall()
        keys.update(str(row["signal_key"]) for row in rows if row["signal_key"])
    except sqlite3.OperationalError:
        pass
    try:
        rows = conn.execute(
            """
            select title, rationale
            from improvement_tasks
            where status = 'open' and priority >= 60
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = f"{row['title']} {row['rationale']}"
        for token in re.findall(r"[A-Z0-9_]+\|frontier_crypto_venue_map\|(?:long|short)_frontier_spot\|(?:standard|conditional)", text):
            keys.add(token)
    return keys


def _candidate_market_keys(row: dict) -> set[str]:
    venue = str(row.get("venue") or "")
    trade_type = str(row.get("trade_type") or "frontier_crypto_venue_map")
    direction = str(row.get("direction") or "")
    route_status = str((row.get("execution_feasibility") or {}).get("status") or "")
    keys = {str(row.get("signal_key") or ""), str(row.get("market_key") or "")}
    if venue and trade_type and direction and route_status:
        keys.add(f"{venue}|{trade_type}|{direction}|{route_status}")
    if venue and trade_type and direction:
        keys.add(f"{venue}|{trade_type}|{direction}")
    if venue and direction:
        keys.add(f"{venue}|{direction}")
    return {key for key in keys if key}


def select_enrichment_observations(
    conn: sqlite3.Connection,
    observations: list[dict],
    variants: list[dict],
    candidates_by_variant: dict[str, list[dict]],
    settings: dict,
) -> list[dict]:
    cfg = settings.get("frontier_data_quality", {})
    base_max_total = int(cfg.get("max_symbols_per_cycle", 60))
    base_max_per_venue = int(cfg.get("max_symbols_per_venue", 12))
    unknown_reserve = int(cfg.get("unknown_quality_reserve_per_cycle", 30))
    regional_reserve = int(cfg.get("regional_reserve_per_cycle", 25))
    exploit_variant_reserve = int(cfg.get("exploit_variant_reserve_per_cycle", 25))
    active_variant_top = int(cfg.get("active_variant_enrichment_top", 10))
    shadow_variant_top = int(cfg.get("shadow_variant_enrichment_top", 2))
    shadow_variant_cap = int(cfg.get("shadow_variant_enrichment_variant_cap", 8))
    starved_venues = {str(venue).upper() for venue in cfg.get("starved_venues", [])}
    venue_minimum_targets = _venue_depth_minimum_targets(cfg, starved_venues)
    adaptive = bool(cfg.get("adaptive_selection", True))
    snapshot_state = _snapshot_rotation_state(conn) if adaptive else {}
    escalation = _quality_target_escalation(
        observations,
        snapshot_state,
        cfg,
        base_max_total,
        base_max_per_venue,
    )
    max_total = int(escalation["max_symbols_per_cycle"])
    max_per_venue = int(escalation["max_symbols_per_venue"])
    if escalation["active"]:
        unknown_reserve += max(0, int(escalation["extra_symbols_requested"]) // 2)
        regional_reserve += max(0, int(escalation["extra_symbols_requested"]) // 4)
    by_id = {str(row.get("instrument_id")): row for row in observations if row.get("instrument_id")}
    selected: list[dict] = []
    selected_ids: set[str] = set()
    venue_counts: collections.Counter[str] = collections.Counter()

    def add_ids(inst_ids: list[str], bucket: str, bucket_limit: int | None = None) -> None:
        added = 0
        for inst_id in inst_ids:
            if len(selected) >= max_total:
                return
            if bucket_limit is not None and added >= bucket_limit:
                return
            row = by_id.get(str(inst_id))
            if not row or str(inst_id) in selected_ids or row.get("data_status") != "reachable":
                continue
            venue = str(row.get("venue"))
            if venue_counts[venue] >= max_per_venue:
                continue
            output = dict(row)
            output["depth_selection_bucket"] = bucket
            output["starved_venue"] = venue.upper() in starved_venues
            output["depth_selection_escalation"] = escalation
            selected.append(output)
            selected_ids.add(str(inst_id))
            venue_counts[venue] += 1
            added += 1

    open_rows = conn.execute(
        """
        select distinct inst_id
        from paper_trades
        where status = 'open' and trade_type = 'frontier_crypto_venue_map'
        order by opened_at asc
        """
    ).fetchall()
    add_ids([str(row["inst_id"]) for row in open_rows], "open_paper_trade")

    exploit_keys = _exploit_more_market_keys(conn) | _open_growth_market_keys(conn)
    active = next((item for item in variants if item.get("status") == "active"), None)
    exploit_variant_ids: list[str] = []
    if active:
        exploit_variant_ids.extend(
            str(row["inst_id"])
            for row in candidates_by_variant.get(active["variant_id"], [])[:active_variant_top]
            if row.get("inst_id")
        )
    shadow_variants_used = 0
    for variant in variants:
        if variant.get("status") not in {"shadow", "retired"}:
            continue
        if variant.get("status") == "shadow" and shadow_variants_used >= shadow_variant_cap:
            continue
        if variant.get("status") == "shadow":
            shadow_variants_used += 1
        exploit_variant_ids.extend(
            str(row["inst_id"])
            for row in candidates_by_variant.get(variant["variant_id"], [])[:shadow_variant_top]
            if row.get("inst_id")
        )
    raw_candidates = [
        row
        for rows in candidates_by_variant.values()
        for row in rows
        if row.get("inst_id")
    ]
    raw_ranked = sorted(
        raw_candidates,
        key=lambda row: abs(float(row.get("venue_deviation_bps") or 0.0)),
        reverse=True,
    )
    exploit_ranked = [
        row
        for row in raw_ranked
        if exploit_keys.intersection(_candidate_market_keys(row))
    ]
    exploit_variant_ids.extend(str(row["inst_id"]) for row in exploit_ranked if row.get("inst_id"))
    add_ids(exploit_variant_ids, "exploit_more_or_variant", exploit_variant_reserve)

    regional_ranked = sorted(
        [row for row in observations if row.get("region") and row.get("instrument_id")],
        key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues)
        if adaptive
        else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
    )
    add_ids([str(row["instrument_id"]) for row in regional_ranked], "regional_frontier", regional_reserve)

    broad_unknown_ranked = sorted(
        [
            row
            for row in observations
            if row.get("instrument_id")
            and row.get("data_status") == "reachable"
            and float(row.get("quote_volume_24h") or 0.0) > 0
            and int((snapshot_state.get(str(row.get("instrument_id"))) or {}).get("consecutive_verified_count") or 0) == 0
        ],
        key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues)
        if adaptive
        else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
    )
    add_ids([str(row["instrument_id"]) for row in broad_unknown_ranked], "unknown_quality_high_volume", unknown_reserve)

    for venue, target in sorted(venue_minimum_targets.items()):
        needed = max(0, target - _venue_count(venue_counts, venue))
        if needed <= 0:
            continue
        venue_ranked = sorted(
            [
                row
                for row in observations
                if row.get("instrument_id")
                and row.get("data_status") == "reachable"
                and str(row.get("venue") or "").upper() == venue
            ],
            key=lambda row: _snapshot_sort_key(row, snapshot_state)
            if adaptive
            else -float(row.get("quote_volume_24h") or 0.0),
        )
        add_ids([str(row["instrument_id"]) for row in venue_ranked], "starved_venue_minimum", needed)

    if escalation["active"] and starved_venues:
        starved_ranked = sorted(
            [
                row
                for row in observations
                if row.get("instrument_id")
                and row.get("data_status") == "reachable"
                and str(row.get("venue") or "").upper() in starved_venues
            ],
            key=lambda row: _snapshot_sort_key(row, snapshot_state)
            if adaptive
            else -float(row.get("quote_volume_24h") or 0.0),
        )
        add_ids(
            [str(row["instrument_id"]) for row in starved_ranked],
            "quality_target_starved_venue",
            int(escalation.get("starved_venue_reserve") or 0),
        )

    add_ids([str(row["inst_id"]) for row in raw_ranked if row.get("inst_id")], "largest_dislocation")
    add_ids(
        [
            str(row["instrument_id"])
            for row in sorted(
                [
                    row
                    for row in observations
                    if row.get("instrument_id")
                    and row.get("data_status") == "reachable"
                    and float(row.get("quote_volume_24h") or 0.0) > 0
                ],
                key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues)
                if adaptive
                else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
            )
        ],
        "rotation_fill",
    )
    quota_report = _selection_quota_report(
        observations,
        selected,
        snapshot_state,
        venue_minimum_targets,
        max_total,
        max_per_venue,
    )
    selection_limits = {
        "max_symbols_per_cycle": max_total,
        "max_symbols_per_venue": max_per_venue,
        "base_max_symbols_per_cycle": base_max_total,
        "base_max_symbols_per_venue": base_max_per_venue,
        "unknown_quality_reserve_per_cycle": unknown_reserve,
        "regional_reserve_per_cycle": regional_reserve,
        "exploit_variant_reserve_per_cycle": exploit_variant_reserve,
    }
    for row in selected:
        row["depth_selection_venue_quota_report"] = quota_report
        row["depth_selection_limits"] = selection_limits
    return selected


def _rolling_latency_baselines(conn: sqlite3.Connection) -> dict[str, float]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """
        select venue, latency_ms
        from frontier_quality_snapshots
        where observed_at >= ? and latency_ms is not null
        """,
        (cutoff,),
    ).fetchall()
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row["venue"])].append(float(row["latency_ms"]))
    return {venue: statistics.median(values) for venue, values in grouped.items() if values}


def enrich_observations(
    conn: sqlite3.Connection,
    observations: list[dict],
    selected: list[dict],
    settings: dict,
    registry: dict,
) -> tuple[list[dict], dict]:
    cfg = settings.get("frontier_data_quality", {})
    if not cfg.get("enabled", True):
        return observations, {"enabled": False}
    targets = _venue_depth_targets(registry)
    timeout = int(cfg.get("timeout_seconds", 6))
    levels = int(cfg.get("depth_levels", 50))
    baselines = _rolling_latency_baselines(conn)
    results: dict[str, dict] = {}

    def fetch_one(observation: dict) -> tuple[str, dict]:
        inst_id = str(observation["instrument_id"])
        target = targets.get(str(observation.get("venue")))
        if not target:
            return inst_id, _unknown_quality(observation, None, "depth_endpoint_not_configured")
        depth_config = target["depth"]
        try:
            url = _build_depth_url(observation, depth_config, levels)
            result = _fetch_json(url, timeout)
            if not result["ok"]:
                return inst_id, _unknown_quality(observation, result, f"depth_{result['status']}")
            extracted = _extract_depth(str(depth_config["parser"]), result["payload"], result["received_at"])
            quality = analyze_book(
                observation,
                extracted,
                latency_ms=float(result["latency_ms"]),
                received_at=str(result["received_at"]),
                max_levels=min(levels, int(depth_config.get("max_levels", levels))),
                baseline_latency_ms=baselines.get(str(observation.get("venue"))),
                fresh_seconds=float(cfg.get("fresh_seconds", 30.0)),
            )
            quality["depth_source_url"] = url
            quality["depth_http_status"] = result["http_status"]
            quality["depth_parser"] = depth_config["parser"]
            return inst_id, quality
        except Exception as exc:  # noqa: BLE001
            return inst_id, _unknown_quality(observation, None, f"depth_enrichment_error:{str(exc)[:120]}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(cfg.get("workers", 8))) as pool:
        futures = [pool.submit(fetch_one, row) for row in selected]
        for future in concurrent.futures.as_completed(futures):
            inst_id, quality = future.result()
            results[inst_id] = quality

    snapshot_state = _snapshot_rotation_state(conn)
    enriched = []
    selected_ids = {str(row["instrument_id"]) for row in selected}
    bucket_counts = collections.Counter(str(row.get("depth_selection_bucket") or "unclassified") for row in selected)
    selected_by_venue = collections.Counter(str(row.get("venue") or "unknown") for row in selected)
    selection_escalation = selected[0].get("depth_selection_escalation", {}) if selected else {}
    selection_quota_report = {
        str(venue): dict(item)
        for venue, item in (selected[0].get("depth_selection_venue_quota_report", {}) if selected else {}).items()
    }
    selection_limits = selected[0].get("depth_selection_limits", {}) if selected else {}
    starved_venues = {
        str(venue).upper()
        for venue in cfg.get("starved_venues", [])
    }
    for row in observations:
        output = dict(row)
        inst_id = str(row.get("instrument_id"))
        quality = results.get(str(row.get("instrument_id")))
        if quality:
            output.update(quality)
        elif inst_id in selected_ids:
            output.update(_unknown_quality(row, None, "depth_result_missing"))
        else:
            output.update(_unknown_quality(row, None, "not_selected_for_depth"))
        prior_count = int((snapshot_state.get(inst_id) or {}).get("consecutive_verified_count") or 0)
        output["verified_depth_snapshot_count"] = (
            prior_count + 1 if output.get("quality_status") == "verified" and inst_id in selected_ids else prior_count
        )
        enriched.append(output)
    target_venues = {str(venue).upper() for venue in targets}
    result_issues_by_venue: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    result_quality_by_venue: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in selected:
        venue = str(row.get("venue") or "unknown").upper()
        quality = results.get(str(row.get("instrument_id"))) or {}
        result_quality_by_venue[venue][str(quality.get("quality_status") or "missing")] += 1
        for flag in quality.get("anomaly_flags") or []:
            if str(flag).startswith("depth_") or flag in {"depth_endpoint_not_configured", "depth_result_missing"}:
                result_issues_by_venue[venue][str(flag)] += 1
    for venue, item in selection_quota_report.items():
        item["depth_endpoint_configured"] = str(venue).upper() in target_venues
        item["selected_quality_status_counts"] = dict(result_quality_by_venue.get(str(venue).upper(), {}))
        item["depth_issue_counts"] = dict(result_issues_by_venue.get(str(venue).upper(), {}))
        if not item["depth_endpoint_configured"] and item.get("observed_count", 0) > 0:
            item["status"] = "missed"
            item["missed_reason"] = "no_depth_endpoint"
    return enriched, {
        "enabled": True,
        "selected_count": len(selected),
        "enriched_count": sum(1 for item in results.values() if item.get("quality_status") in {"verified", "degraded"}),
        "unknown_count": sum(1 for item in results.values() if item.get("quality_status") == "unknown"),
        "blocked_count": sum(1 for item in results.values() if item.get("quality_status") == "blocked"),
        "selected_instruments": [row.get("instrument_id") for row in selected],
        "selection_escalation": selection_escalation,
        "selection_limits": selection_limits,
        "worker_count": int(cfg.get("workers", 8)),
        "venue_quota_report": selection_quota_report,
        "selection_bucket_counts": dict(bucket_counts),
        "selected_by_venue": dict(selected_by_venue),
        "starved_venues": sorted(starved_venues),
        "selected_starved_venue_count": sum(
            count
            for venue, count in selected_by_venue.items()
            if str(venue).upper() in starved_venues
        ),
        "starved_selected_by_venue": {
            venue: count
            for venue, count in sorted(selected_by_venue.items())
            if str(venue).upper() in starved_venues
        },
    }


def persist_quality_snapshots(
    conn: sqlite3.Connection,
    observations: list[dict],
    settings: dict,
) -> dict:
    cfg = settings.get("frontier_data_quality", {})
    now = dt.datetime.now(dt.timezone.utc)
    bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).isoformat()
    inserted = 0
    for row in observations:
        if "not_selected_for_depth" in (row.get("anomaly_flags") or []):
            continue
        if row.get("quality_status") not in {"verified", "degraded", "unknown", "blocked"}:
            continue
        fills = row.get("simulated_fills") or {}
        depth = row.get("depth_usd") or {}
        conn.execute(
            """
            insert into frontier_quality_snapshots (
                bucket_at, observed_at, venue, inst_id, quality_status,
                quality_score, venue_quality_score, latency_ms,
                freshness_age_seconds, spread_bps, bid_depth_10bps_usd,
                ask_depth_10bps_usd, buy_slippage_1000_bps,
                sell_slippage_1000_bps, anomaly_json, metrics_json
            ) values (?, ?, ?, ?, ?, ?, null, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(bucket_at, inst_id) do update set
                observed_at = excluded.observed_at,
                quality_status = excluded.quality_status,
                quality_score = excluded.quality_score,
                latency_ms = excluded.latency_ms,
                freshness_age_seconds = excluded.freshness_age_seconds,
                spread_bps = excluded.spread_bps,
                bid_depth_10bps_usd = excluded.bid_depth_10bps_usd,
                ask_depth_10bps_usd = excluded.ask_depth_10bps_usd,
                buy_slippage_1000_bps = excluded.buy_slippage_1000_bps,
                sell_slippage_1000_bps = excluded.sell_slippage_1000_bps,
                anomaly_json = excluded.anomaly_json,
                metrics_json = excluded.metrics_json
            """,
            (
                bucket,
                row.get("book_observed_at") or _utc_now(),
                row.get("venue"),
                row.get("instrument_id"),
                row.get("quality_status"),
                row.get("quality_score"),
                row.get("depth_latency_ms"),
                row.get("freshness_age_seconds"),
                row.get("spread_bps"),
                ((depth.get("bid") or {}).get("10")),
                ((depth.get("ask") or {}).get("10")),
                (((fills.get("buy") or {}).get("1000") or {}).get("slippage_bps")),
                (((fills.get("sell") or {}).get("1000") or {}).get("slippage_bps")),
                json.dumps(row.get("anomaly_flags") or [], sort_keys=True),
                json.dumps(
                    {
                        "quality_components": row.get("quality_components") or {},
                        "depth_usd": depth,
                        "simulated_fills": fills,
                        "freshness_basis": row.get("freshness_basis"),
                        "depth_concentration_25bps": row.get("depth_concentration_25bps"),
                        "book_imbalance_10bps": row.get("book_imbalance_10bps"),
                    },
                    sort_keys=True,
                ),
            ),
        )
        inserted += 1
    max_rows = int(cfg.get("snapshot_retention_rows", 100000))
    total = int(conn.execute("select count(*) as n from frontier_quality_snapshots").fetchone()["n"])
    deleted = 0
    if total > max_rows:
        deleted = total - max_rows
        conn.execute(
            """
            delete from frontier_quality_snapshots
            where id in (
                select id from frontier_quality_snapshots
                order by bucket_at asc, id asc
                limit ?
            )
            """,
            (deleted,),
        )
    conn.commit()
    return {"snapshot_rows_written": inserted, "snapshot_rows_deleted": deleted, "retention_limit": max_rows}


def venue_quality_scores(conn: sqlite3.Connection) -> list[dict]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """
        select venue, quality_status, quality_score, latency_ms,
               freshness_age_seconds, anomaly_json
        from frontier_quality_snapshots
        where observed_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row["venue"])].append(dict(row))
    output = []
    for venue, items in grouped.items():
        reachable = sum(item["quality_status"] in {"verified", "degraded"} for item in items) / len(items)
        freshness = statistics.fmean(
            max(
                0.0,
                1.0
                - float(
                    item["freshness_age_seconds"]
                    if item["freshness_age_seconds"] is not None
                    else 90.0
                )
                / 90.0,
            )
            for item in items
        )
        scores = [float(item["quality_score"]) for item in items if item["quality_score"] is not None]
        median_quality = statistics.median(scores) / 100.0 if scores else 0.0
        anomaly_free = sum(not json.loads(item["anomaly_json"] or "[]") for item in items) / len(items)
        latencies = [float(item["latency_ms"]) for item in items if item["latency_ms"] is not None]
        if latencies:
            median_latency = statistics.median(latencies)
            dispersion = statistics.pstdev(latencies) if len(latencies) > 1 else 0.0
            latency_stability = max(0.0, 1.0 - dispersion / max(median_latency, 1.0))
        else:
            median_latency = None
            latency_stability = 0.0
        score = (
            reachable * 30.0
            + freshness * 25.0
            + median_quality * 20.0
            + anomaly_free * 15.0
            + latency_stability * 10.0
        )
        output.append(
            {
                "venue": venue,
                "venue_quality_score": round(score, 3),
                "snapshot_count": len(items),
                "reachability_rate": round(reachable, 3),
                "freshness_score": round(freshness, 3),
                "median_instrument_quality": round(median_quality * 100.0, 3),
                "anomaly_free_rate": round(anomaly_free, 3),
                "median_latency_ms": round(median_latency, 3) if median_latency is not None else None,
                "latency_stability": round(latency_stability, 3),
            }
        )
    output.sort(key=lambda item: item["venue_quality_score"], reverse=True)
    return output


def market_testing_progress(conn: sqlite3.Connection) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    windows = {
        "last_hour": now - dt.timedelta(hours=1),
        "last_24h": now - dt.timedelta(hours=24),
    }
    output: dict[str, dict] = {}
    for label, cutoff in windows.items():
        rows = conn.execute(
            """
            select venue, inst_id, quality_status
            from frontier_quality_snapshots
            where observed_at >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        venues = {str(row["venue"]) for row in rows}
        instruments = {str(row["inst_id"]) for row in rows}
        known = {
            str(row["inst_id"])
            for row in rows
            if row["quality_status"] in {"verified", "degraded"}
        }
        new_market_rows = conn.execute(
            """
            select venue, inst_id, min(observed_at) as first_seen
            from frontier_quality_snapshots
            group by venue, inst_id
            having first_seen >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        new_venue_rows = conn.execute(
            """
            select venue, min(observed_at) as first_seen
            from frontier_quality_snapshots
            group by venue
            having first_seen >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        output[label] = {
            "venues_tested": len(venues),
            "markets_tested": len(instruments),
            "new_venues_tested": len({str(row["venue"]) for row in new_venue_rows}),
            "new_markets_tested": len({f"{row['venue']}:{row['inst_id']}" for row in new_market_rows}),
            "known_quality_markets": len(known),
            "quality_test_count": len(rows),
        }
    return output


def quality_outcome_relationship(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select o.pnl_bps, p.candidate_json
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.horizon_minutes = 60
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
          and p.trade_type = 'frontier_crypto_venue_map'
        """
    ).fetchall()
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        candidate = json.loads(row["candidate_json"] or "{}")
        score = candidate.get("quality_score")
        if score is None:
            continue
        score = float(score)
        bucket = "0-34" if score < 35 else "35-59" if score < 60 else "60-79" if score < 80 else "80-100"
        grouped[bucket].append(float(row["pnl_bps"]))
    output = []
    for bucket in ("0-34", "35-59", "60-79", "80-100"):
        values = grouped.get(bucket, [])
        if not values:
            continue
        output.append(
            {
                "quality_bucket": bucket,
                "closed_count": len(values),
                "avg_pnl_bps": round(statistics.fmean(values), 3),
                "win_rate": round(sum(value > 0 for value in values) / len(values), 3),
            }
        )
    return output
