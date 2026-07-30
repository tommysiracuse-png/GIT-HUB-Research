#!/usr/bin/env python3
"""Public prediction-market scanner for Polymarket and Kalshi.

This adapter is public-data only. It creates paper-testable candidates but does
not place orders or require credentials.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import pathlib
import urllib.parse
import urllib.request

from scan_batch import ScanBatch, observation_from_candidate


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"


def fetch_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 inefficiency-radar/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def as_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def liquidity_score(liquidity: float) -> float:
    if liquidity <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(liquidity) - 2.0) / 5.0))


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _bucket(value: float, cutoffs: list[tuple[float, str]], default: str) -> str:
    for threshold, label in cutoffs:
        if value < threshold:
            return label
    return default


def _end_date_bucket(value: object) -> str:
    if not value:
        return "unknown"
    try:
        end = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        days = (end - now).total_seconds() / 86400.0
    except ValueError:
        return "unknown"
    if days < 0:
        return "expired_or_resolution_pending"
    if days <= 1:
        return "0_1d"
    if days <= 7:
        return "1_7d"
    if days <= 30:
        return "7_30d"
    return "30d_plus"


def _event_tags(row: dict) -> list[str]:
    return _event_tag_details(row)["tags"]


def _event_tag_details(row: dict) -> dict:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("question", "title", "slug", "category", "description")
    ).lower()
    tags = []
    for tag, terms in {
        "politics": ("election", "president", "trump", "senate", "congress", "government"),
        "macro": ("fed", "inflation", "rate", "cpi", "gdp", "jobs", "unemployment"),
        "earnings_company": ("earnings", "revenue", "profit", "ipo", "stock", "company", "tesla", "nvidia"),
        "regulation": ("sec", "lawsuit", "ban", "approve", "approval", "regulation", "court", "tariff"),
        "sports": ("nba", "nfl", "mlb", "soccer", "champion", "game"),
        "crypto": ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana"),
        "geopolitics": ("war", "ceasefire", "china", "taiwan", "ukraine", "israel"),
        "weather": ("hurricane", "temperature", "weather", "rain", "storm"),
        "regional": ("africa", "nigeria", "south africa", "brazil", "mexico", "argentina", "indonesia"),
    }.items():
        if any(term in text for term in terms):
            tags.append(tag)
    if not tags:
        return {"tags": ["uncategorized"], "confidence": 0.25}
    return {"tags": tags, "confidence": min(0.95, 0.55 + len(tags) * 0.12)}


def _resolution_risk_status(end_date: object) -> str:
    bucket = _end_date_bucket(end_date)
    if bucket == "expired_or_resolution_pending":
        return "expired_or_resolution_pending"
    if bucket == "unknown":
        return "unclear_resolution_date"
    if bucket == "0_1d":
        return "near_resolution"
    return "normal"


def _prediction_risk_flags(end_date: object, spread_bps: float, liquidity: float, metadata: dict) -> list[str]:
    flags = []
    bucket = _end_date_bucket(end_date)
    if bucket in {"unknown", "expired_or_resolution_pending"}:
        flags.append("resolution_date_unclear")
    if spread_bps >= 500.0:
        flags.append("wide_prediction_spread")
    if liquidity <= 100.0:
        flags.append("thin_prediction_liquidity")
    if metadata.get("orderbook_status") in {"blocked", "unavailable", "error"}:
        flags.append("orderbook_not_verified")
    if "uncategorized" in metadata.get("event_tags", []):
        flags.append("needs_llm_event_classification")
    if metadata.get("resolution_risk_status") in {"expired_or_resolution_pending", "unclear_resolution_date"}:
        flags.append(str(metadata["resolution_risk_status"]))
    return flags


def _days_to_end(end_date: object) -> float | None:
    if not end_date:
        return None
    try:
        end = dt.datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    return (end - now).total_seconds() / 86400.0


def _polymarket_paper_gate(candidate: dict, row: dict, settings: dict) -> tuple[bool, list[str]]:
    config = settings.get("prediction_market_scanner", {}) or {}
    if not config.get("polymarket_paper_gate_enabled", True):
        return True, []
    metadata = candidate.get("data_source") or {}
    reasons = []
    days_to_end = _days_to_end(row.get("endDate") or metadata.get("endDate"))
    max_days = as_float(config.get("polymarket_max_days_to_resolution"), 30.0)
    min_liquidity = as_float(config.get("polymarket_min_liquidity_usd"), 1_000.0)
    max_spread_bps = as_float(config.get("polymarket_max_spread_bps"), 300.0)
    if days_to_end is None:
        reasons.append("missing_outcome_timestamp")
    elif days_to_end < 0:
        reasons.append("expired_or_resolution_pending")
    elif max_days > 0 and days_to_end > max_days:
        reasons.append("too_far_from_resolution")
    if config.get("polymarket_require_visible_book", True):
        if metadata.get("orderbook_status") != "verified":
            reasons.append("missing_verified_orderbook")
        if as_float(metadata.get("orderbook_best_bid")) <= 0 or as_float(metadata.get("orderbook_best_ask")) <= 0:
            reasons.append("missing_visible_bid_ask")
    if max_spread_bps > 0 and as_float(candidate.get("spread_bps")) > max_spread_bps:
        reasons.append("paper_spread_too_wide")
    liquidity_proxy = max(candidate.get("quote_volume_24h"), as_float(row.get("liquidityNum") or row.get("liquidity")), as_float(metadata.get("orderbook_depth_usd")))
    if min_liquidity > 0 and liquidity_proxy < min_liquidity:
        reasons.append("paper_liquidity_too_thin")
    return not reasons, reasons


def _book_depth_from_levels(levels: list, *, price_scale: float = 1.0) -> float:
    total = 0.0
    for level in levels[:20]:
        if isinstance(level, dict):
            price = as_float(level.get("price") or level.get("p"))
            size = as_float(level.get("size") or level.get("q") or level.get("quantity"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = as_float(level[0])
            size = as_float(level[1])
        else:
            continue
        if price and size:
            total += price * price_scale * size
    return round(total, 3)


def _polymarket_orderbook(token_id: object) -> dict:
    if not token_id:
        return {"orderbook_status": "missing_token"}
    try:
        row = fetch_json("https://clob.polymarket.com/book?" + urllib.parse.urlencode({"token_id": str(token_id)}), timeout=8)
        bids = row.get("bids") or []
        asks = row.get("asks") or []
        best_bid = max([as_float(item.get("price") if isinstance(item, dict) else item[0]) for item in bids] or [0.0])
        best_ask_values = [as_float(item.get("price") if isinstance(item, dict) else item[0]) for item in asks]
        best_ask_values = [value for value in best_ask_values if value]
        best_ask = min(best_ask_values) if best_ask_values else 0.0
        spread = (best_ask - best_bid) * 10_000.0 if best_ask > best_bid > 0 else None
        return {
            "orderbook_status": "verified" if bids or asks else "empty",
            "orderbook_best_bid": round(best_bid, 6) if best_bid else None,
            "orderbook_best_ask": round(best_ask, 6) if best_ask else None,
            "orderbook_spread_bps": round(spread, 3) if spread is not None else None,
            "orderbook_depth_usd": round(_book_depth_from_levels(bids) + _book_depth_from_levels(asks), 3),
            "orderbook_source": "Polymarket CLOB public book",
        }
    except Exception as exc:  # noqa: BLE001
        return {"orderbook_status": "error", "orderbook_error": str(exc)[:160]}


def _kalshi_orderbook(ticker: object) -> dict:
    if not ticker:
        return {"orderbook_status": "missing_ticker"}
    try:
        row = fetch_json(
            f"https://external-api.kalshi.com/trade-api/v2/markets/{urllib.parse.quote(str(ticker))}/orderbook",
            timeout=8,
        )
        body = row.get("orderbook") or row.get("market_orderbook") or row
        yes = body.get("yes") or body.get("yes_bids") or []
        no = body.get("no") or body.get("no_bids") or []
        return {
            "orderbook_status": "verified" if yes or no else "empty",
            "orderbook_depth_usd": round(_book_depth_from_levels(yes, price_scale=0.01) + _book_depth_from_levels(no, price_scale=0.01), 3),
            "orderbook_source": "Kalshi public market orderbook",
        }
    except Exception as exc:  # noqa: BLE001
        return {"orderbook_status": "error", "orderbook_error": str(exc)[:160]}


def feasibility(settings: dict) -> dict:
    allowed = settings.get("account_capabilities", {}).get("prediction_markets", False)
    return {
        "status": "standard" if allowed else "conditional",
        "requires_short_spot": False,
        "legs": ["buy YES or NO event contract"],
        "notes": [
            "Prediction-market execution requires jurisdiction, account, API, and venue eligibility checks.",
            "Paper mode may study these markets before a live route is configured.",
        ],
    }


def _candidate(
    venue: str,
    inst_id: str,
    question: str,
    price: float,
    direction: str,
    liquidity: float,
    volume_24h: float,
    spread_bps: float,
    change_hint_bps: float,
    settings: dict,
    metadata: dict,
) -> dict:
    liq = liquidity_score(max(liquidity, volume_24h))
    if metadata.get("orderbook_spread_bps") is not None:
        spread_bps = min(spread_bps, float(metadata["orderbook_spread_bps"]))
    if metadata.get("orderbook_depth_usd"):
        liquidity = max(liquidity, float(metadata["orderbook_depth_usd"]))
        liq = liquidity_score(max(liquidity, volume_24h))
    edge = max(0.0, min(abs(change_hint_bps) * 0.25 + liq * 12.0 - spread_bps * 0.2, 40.0))
    score = round(max(0.0, min(100.0, edge + liq * 35.0 - min(spread_bps * 0.15, 20.0))), 3)
    metadata.setdefault("event_tags", [])
    metadata["end_date_bucket"] = _end_date_bucket(metadata.get("endDate") or metadata.get("close_time"))
    metadata["resolution_risk_status"] = _resolution_risk_status(metadata.get("endDate") or metadata.get("close_time"))
    metadata.setdefault("event_tag_confidence", 0.25 if "uncategorized" in metadata.get("event_tags", []) else 0.75)
    metadata["liquidity_bucket"] = _bucket(max(liquidity, volume_24h), [(100, "tiny"), (1_000, "thin"), (10_000, "moderate"), (100_000, "deep")], "very_deep")
    metadata["volume_bucket"] = _bucket(volume_24h, [(100, "tiny"), (1_000, "thin"), (10_000, "moderate"), (100_000, "active")], "very_active")
    metadata["spread_bucket"] = _bucket(spread_bps, [(50, "tight"), (200, "normal"), (500, "wide")], "very_wide")
    risk_flags = _prediction_risk_flags(metadata.get("endDate") or metadata.get("close_time"), spread_bps, liquidity, metadata)
    metadata["llm_event_review_needed"] = bool(
        "uncategorized" in metadata.get("event_tags", [])
        or metadata["resolution_risk_status"] != "normal"
        or score >= 65.0
    )
    return {
        "seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "venue": venue,
        "inst_id": inst_id,
        "name": question[:180],
        "region": "prediction_market",
        "asset_class": "event_contract",
        "trade_type": "prediction_market_probability",
        "direction": direction,
        "execution_feasibility": feasibility(settings),
        "thesis": "prediction market probability movement/liquidity candidate for LLM event-latency review",
        "last": round(max(0.01, min(0.99, price)), 6),
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "edge_bps_estimate": round(edge, 3),
        "change_24h_pct": round(change_hint_bps / 100.0, 3),
        "quote_volume_24h": round(volume_24h, 3),
        "liquidity_score": round(liq, 3),
        "spread_bps": round(spread_bps, 3),
        "score": score,
        "risk_notes": [
            "paper-trade only",
            "resolution rules and venue eligibility must be checked before live trading",
            "price movement alone is not an edge; LLM/catalyst review is required",
            *risk_flags,
        ],
        "data_source": metadata,
    }


def _polymarket_candidate_from_row(row: dict, settings: dict, orderbook: dict) -> dict | None:
    try:
        prices = json.loads(row.get("outcomePrices") or "[]")
    except json.JSONDecodeError:
        prices = []
    yes = as_float(prices[0] if prices else row.get("lastTradePrice"), 0.5)
    no = max(0.01, min(0.99, 1.0 - yes))
    one_week = as_float(row.get("oneWeekPriceChange"), 0.0) * 10_000.0
    direction = "buy_yes_event" if one_week >= 0 else "buy_no_event"
    token_ids = _json_list(row.get("clobTokenIds") or row.get("clobTokenIDs"))
    price = yes if direction == "buy_yes_event" else no
    spread_bps = as_float(row.get("spread"), 0.03) * 10_000.0
    tag_details = _event_tag_details(row)
    metadata = {
        "provider": "Polymarket Gamma API",
        "slug": row.get("slug"),
        "market_id": row.get("id"),
        "endDate": row.get("endDate"),
        "outcome_timestamp_present": bool(row.get("endDate")),
        "has_orderbook_token": bool(token_ids),
        "event_tags": tag_details["tags"],
        "event_tag_confidence": tag_details["confidence"],
        **orderbook,
    }
    return _candidate(
        "POLYMARKET",
        f"poly:{row.get('id')}",
        row.get("question") or row.get("slug") or "Polymarket market",
        price,
        direction,
        as_float(row.get("liquidityNum") or row.get("liquidity")),
        as_float(row.get("volume24hr") or row.get("volume24hrClob")),
        spread_bps,
        one_week,
        settings,
        metadata,
    )


def _polymarket_candidates(settings: dict, limit: int) -> tuple[list[dict], dict]:
    url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode(
        {"active": "true", "closed": "false", "limit": str(limit)}
    )
    rows = fetch_json(url)
    preliminary = []
    expired_filtered = 0
    enrich_top = int(settings.get("prediction_market_scanner", {}).get("orderbook_enrichment_top", 10))
    paper_gate_filtered = 0
    paper_gate_reasons = collections.Counter()
    row_by_id = {}
    for row in rows:
        if _end_date_bucket(row.get("endDate")) == "expired_or_resolution_pending":
            expired_filtered += 1
            continue
        candidate = _polymarket_candidate_from_row(row, settings, {"orderbook_status": "not_selected_for_depth"})
        if candidate:
            preliminary.append(candidate)
            row_by_id[candidate["inst_id"]] = row
    preliminary.sort(
        key=lambda item: (
            (item.get("data_source") or {}).get("llm_event_review_needed") is True,
            item.get("score", 0.0),
            item.get("liquidity_score", 0.0),
        ),
        reverse=True,
    )
    enriched_ids = {row["inst_id"] for row in preliminary[:enrich_top]}
    candidates = []
    for candidate in preliminary:
        row = row_by_id[candidate["inst_id"]]
        orderbook = {"orderbook_status": "not_selected_for_depth"}
        if candidate["inst_id"] in enriched_ids:
            token_ids = _json_list(row.get("clobTokenIds") or row.get("clobTokenIDs"))
            orderbook = _polymarket_orderbook(token_ids[0] if token_ids else None)
        rebuilt = _polymarket_candidate_from_row(row, settings, orderbook)
        if rebuilt:
            allowed, reasons = _polymarket_paper_gate(rebuilt, row, settings)
            rebuilt_source = rebuilt.get("data_source") or {}
            rebuilt_source["paper_gate_status"] = "pass" if allowed else "filtered"
            rebuilt_source["paper_gate_reasons"] = reasons
            rebuilt["data_source"] = rebuilt_source
            if allowed:
                candidates.append(rebuilt)
            else:
                paper_gate_filtered += 1
                paper_gate_reasons.update(reasons or ["filtered_unspecified"])
    return candidates, {
        "provider": "POLYMARKET",
        "fetched_count": len(rows),
        "expired_filtered_count": expired_filtered,
        "candidate_count": len(candidates),
        "orderbook_enrichment_top": enrich_top,
        "paper_gate_enabled": bool((settings.get("prediction_market_scanner", {}) or {}).get("polymarket_paper_gate_enabled", True)),
        "paper_gate_filtered_count": paper_gate_filtered,
        "paper_gate_reason_counts": dict(paper_gate_reasons),
    }


def polymarket_candidates(settings: dict, limit: int) -> list[dict]:
    candidates, _status = _polymarket_candidates(settings, limit)
    return candidates


def _kalshi_candidate_from_row(row: dict, settings: dict, orderbook: dict) -> dict | None:
    close_time = row.get("close_time")
    yes_bid = as_float(row.get("yes_bid"), 0.0) / 100.0
    yes_ask = as_float(row.get("yes_ask"), 0.0) / 100.0
    no_bid = as_float(row.get("no_bid"), 0.0) / 100.0
    no_ask = as_float(row.get("no_ask"), 0.0) / 100.0
    last = as_float(row.get("last_price"), 0.0) / 100.0
    if last <= 0 and yes_bid > 0 and yes_ask > 0:
        last = (yes_bid + yes_ask) / 2.0
    if last <= 0:
        return None
    spread_bps = (yes_ask - yes_bid) * 10_000.0 if yes_ask > yes_bid > 0 else 500.0
    volume = as_float(row.get("volume_24h") or row.get("volume"))
    open_interest = as_float(row.get("open_interest"))
    tag_details = _event_tag_details(row)
    metadata = {
        "provider": "Kalshi public API",
        "ticker": row.get("ticker"),
        "title": row.get("title"),
        "category": row.get("category"),
        "close_time": close_time,
        "yes_bid": yes_bid if yes_bid > 0 else None,
        "yes_ask": yes_ask if yes_ask > 0 else None,
        "no_bid": no_bid if no_bid > 0 else None,
        "no_ask": no_ask if no_ask > 0 else None,
        "settlement_status": row.get("settlement_status") or row.get("status"),
        "open_interest": open_interest,
        "event_tags": tag_details["tags"],
        "event_tag_confidence": tag_details["confidence"],
        **orderbook,
    }
    return _candidate(
        "KALSHI",
        f"kalshi:{row.get('ticker')}",
        row.get("title") or row.get("ticker") or "Kalshi market",
        last,
        "buy_yes_event",
        open_interest,
        volume,
        spread_bps,
        as_float(row.get("price_delta_24h"), 0.0),
        settings,
        metadata,
    )


def _kalshi_candidates(settings: dict, limit: int) -> tuple[list[dict], dict]:
    limit = min(int(limit), int(settings.get("prediction_market_scanner", {}).get("kalshi_market_cap", 50)))
    url = "https://external-api.kalshi.com/trade-api/v2/markets?" + urllib.parse.urlencode({"limit": str(limit)})
    data = fetch_json(url)
    preliminary = []
    expired_filtered = 0
    enrich_top = int(settings.get("prediction_market_scanner", {}).get("orderbook_enrichment_top", 10))
    row_by_id = {}
    for row in data.get("markets", []):
        close_time = row.get("close_time")
        if _end_date_bucket(close_time) == "expired_or_resolution_pending":
            expired_filtered += 1
            continue
        candidate = _kalshi_candidate_from_row(row, settings, {"orderbook_status": "not_selected_for_depth"})
        if candidate:
            preliminary.append(candidate)
            row_by_id[candidate["inst_id"]] = row
    preliminary.sort(
        key=lambda item: (
            (item.get("data_source") or {}).get("llm_event_review_needed") is True,
            item.get("score", 0.0),
            item.get("liquidity_score", 0.0),
        ),
        reverse=True,
    )
    enriched_ids = {row["inst_id"] for row in preliminary[:enrich_top]}
    candidates = []
    for candidate in preliminary:
        row = row_by_id[candidate["inst_id"]]
        orderbook = {"orderbook_status": "not_selected_for_depth"}
        if candidate["inst_id"] in enriched_ids:
            orderbook = _kalshi_orderbook(row.get("ticker"))
        rebuilt = _kalshi_candidate_from_row(row, settings, orderbook)
        if rebuilt:
            candidates.append(rebuilt)
    return candidates, {
        "provider": "KALSHI",
        "fetched_count": len(data.get("markets", [])),
        "expired_filtered_count": expired_filtered,
        "candidate_count": len(candidates),
        "orderbook_enrichment_top": enrich_top,
    }


def kalshi_candidates(settings: dict, limit: int) -> list[dict]:
    candidates, _status = _kalshi_candidates(settings, limit)
    return candidates


def _required_observation(inst_id: str) -> dict | None:
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        if inst_id.startswith("poly:"):
            market_id = inst_id.split(":", 1)[1]
            row = fetch_json(f"https://gamma-api.polymarket.com/markets/{urllib.parse.quote(market_id)}")
            prices = json.loads(row.get("outcomePrices") or "[]")
            price = as_float(prices[0] if prices else row.get("lastTradePrice"), 0.0)
            if price > 0:
                return {
                    "inst_id": inst_id,
                    "venue": "POLYMARKET",
                    "trade_type": "prediction_market_probability",
                    "last": price,
                    "observed_at": observed_at,
                    "price_source": "Polymarket Gamma API direct market",
                }
        if inst_id.startswith("kalshi:"):
            ticker = inst_id.split(":", 1)[1]
            payload = fetch_json(
                f"https://external-api.kalshi.com/trade-api/v2/markets/{urllib.parse.quote(ticker)}"
            )
            row = payload.get("market") or payload
            price = as_float(row.get("last_price"), 0.0) / 100.0
            if price > 0:
                return {
                    "inst_id": inst_id,
                    "venue": "KALSHI",
                    "trade_type": "prediction_market_probability",
                    "last": price,
                    "observed_at": observed_at,
                    "price_source": "Kalshi public API direct market",
                }
    except Exception:
        return None
    return None


def build_scan_batch(
    settings: dict,
    limit: int = 40,
    required_inst_ids: set[str] | None = None,
) -> ScanBatch:
    candidates = []
    provider_status = []
    provider_cap = int(settings.get("prediction_market_scanner", {}).get("provider_market_cap", max(5, limit)))
    per_provider = max(5, min(provider_cap, limit))
    for builder in (_polymarket_candidates, _kalshi_candidates):
        try:
            built, status = builder(settings, per_provider)
            candidates.extend(built)
            provider_status.append(status)
        except Exception as exc:  # noqa: BLE001
            provider_status.append(
                {
                    "provider": builder.__name__.replace("_candidates", "").strip("_").upper(),
                    "status": "error",
                    "error": str(exc)[:160],
                    "candidate_count": 0,
                    "expired_filtered_count": 0,
                }
            )
            candidates.append(
                {
                    "seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "venue": "PREDICTION_SCANNER",
                    "inst_id": f"error:{builder.__name__}",
                    "trade_type": "scanner_error",
                    "direction": "watch_only",
                    "execution_feasibility": {"status": "watch_only"},
                    "thesis": f"{builder.__name__} failed: {exc}",
                    "last": 0.0,
                    "funding_bps": 0.0,
                    "basis_bps": 0.0,
                    "change_24h_pct": 0.0,
                    "quote_volume_24h": 0.0,
                    "liquidity_score": 0.0,
                    "spread_bps": 999.0,
                    "score": 0.0,
                }
            )
    candidates.sort(key=lambda row: row.get("score", 0), reverse=True)
    observations = [
        observation_from_candidate(candidate, source=f"{candidate.get('venue')} public API")
        for candidate in candidates
        if float(candidate.get("last") or 0.0) > 0
    ]
    observed_ids = {row["inst_id"] for row in observations}
    for inst_id in sorted(required_inst_ids or set()):
        if inst_id in observed_ids:
            continue
        observation = _required_observation(inst_id)
        if observation:
            observations.append(observation)
    selected = candidates[:limit]
    scan_metadata = {"provider_status": provider_status}
    write_outputs(selected, observations, settings, scan_metadata=scan_metadata)
    summary = summarize(selected, scan_metadata=scan_metadata)
    return ScanBatch(
        source="Public prediction market APIs",
        candidates=selected,
        observations=observations,
        metadata={"priced_instrument_count": len(observations), "prediction_market_summary": summary},
    )


def build_candidates(settings: dict, limit: int = 40) -> list[dict]:
    return build_scan_batch(settings, limit=limit).candidates


def summarize(candidates: list[dict], scan_metadata: dict | None = None) -> dict:
    scan_metadata = scan_metadata or {}
    by_venue = collections.Counter(row.get("venue", "unknown") for row in candidates)
    by_route = collections.Counter((row.get("execution_feasibility") or {}).get("status", "unknown") for row in candidates)
    by_orderbook = collections.Counter((row.get("data_source") or {}).get("orderbook_status", "unknown") for row in candidates)
    by_tag: collections.Counter[str] = collections.Counter()
    by_end_date = collections.Counter()
    by_spread = collections.Counter()
    by_liquidity = collections.Counter()
    by_confidence = collections.Counter()
    by_resolution_risk = collections.Counter()
    blockers: collections.Counter[str] = collections.Counter()
    for row in candidates:
        source = row.get("data_source") or {}
        by_tag.update(source.get("event_tags") or ["uncategorized"])
        by_end_date[source.get("end_date_bucket", "unknown")] += 1
        by_spread[source.get("spread_bucket", "unknown")] += 1
        by_liquidity[source.get("liquidity_bucket", "unknown")] += 1
        confidence = float(source.get("event_tag_confidence") or 0.0)
        by_confidence[_bucket(confidence, [(0.4, "low"), (0.75, "medium"), (0.9, "high")], "very_high")] += 1
        by_resolution_risk[source.get("resolution_risk_status", "unknown")] += 1
        if (row.get("execution_feasibility") or {}).get("status") == "conditional":
            blockers.update(["prediction_markets_account", "venue_api_access", "jurisdiction_eligibility"])
    event_review_queue = [
        {
            "inst_id": row.get("inst_id"),
            "venue": row.get("venue"),
            "name": row.get("name"),
            "direction": row.get("direction"),
            "score": row.get("score"),
            "last": row.get("last"),
            "event_tags": (row.get("data_source") or {}).get("event_tags", []),
            "event_tag_confidence": (row.get("data_source") or {}).get("event_tag_confidence"),
            "resolution_risk_status": (row.get("data_source") or {}).get("resolution_risk_status"),
            "orderbook_status": (row.get("data_source") or {}).get("orderbook_status"),
            "liquidity_bucket": (row.get("data_source") or {}).get("liquidity_bucket"),
            "spread_bucket": (row.get("data_source") or {}).get("spread_bucket"),
        }
        for row in sorted(
            [item for item in candidates if (item.get("data_source") or {}).get("llm_event_review_needed")],
            key=lambda item: item.get("score", 0.0),
            reverse=True,
        )[:10]
    ]
    event_review_shadow_trials = [
        {
            "trial_id": f"event_review:{row.get('venue')}:{row.get('inst_id')}",
            "status": "shadow_only",
            "inst_id": row.get("inst_id"),
            "venue": row.get("venue"),
            "event_tags": (row.get("data_source") or {}).get("event_tags", []),
            "event_tag_confidence": (row.get("data_source") or {}).get("event_tag_confidence"),
            "resolution_risk_status": (row.get("data_source") or {}).get("resolution_risk_status"),
            "liquidity_bucket": (row.get("data_source") or {}).get("liquidity_bucket"),
            "spread_bucket": (row.get("data_source") or {}).get("spread_bucket"),
            "orderbook_status": (row.get("data_source") or {}).get("orderbook_status"),
            "score": row.get("score"),
            "paper_action": "review_event_classification_only",
        }
        for row in sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:10]
    ]
    research_queue = {
        "shadow_only": True,
        "group_counts": {
            "by_event_tag": dict(by_tag),
            "by_spread_bucket": dict(by_spread),
            "by_liquidity_bucket": dict(by_liquidity),
            "by_resolution_risk_status": dict(by_resolution_risk),
            "by_orderbook_status": dict(by_orderbook),
        },
        "review_queue_count": len(event_review_queue),
        "shadow_trial_count": len(event_review_shadow_trials),
        "review_items": event_review_queue,
    }
    provider_status = scan_metadata.get("provider_status", [])
    expired_filtered_count = sum(int(item.get("expired_filtered_count") or 0) for item in provider_status)
    return {
        "candidate_count": len(candidates),
        "expired_filtered_count": expired_filtered_count,
        "provider_status": provider_status,
        "by_venue": dict(by_venue),
        "by_route_status": dict(by_route),
        "by_orderbook_status": dict(by_orderbook),
        "by_event_tag": dict(by_tag),
        "by_event_tag_confidence": dict(by_confidence),
        "by_end_date_bucket": dict(by_end_date),
        "by_resolution_risk_status": dict(by_resolution_risk),
        "by_spread_bucket": dict(by_spread),
        "by_liquidity_bucket": dict(by_liquidity),
        "route_blockers": dict(blockers),
        "prediction_event_review_queue": event_review_queue,
        "prediction_market_research_queue": research_queue,
        "event_review_shadow_trials": event_review_shadow_trials,
        "top_candidates": [
            {
                "inst_id": row.get("inst_id"),
                "venue": row.get("venue"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "last": row.get("last"),
                "spread_bps": row.get("spread_bps"),
                "liquidity_score": row.get("liquidity_score"),
                "event_tags": (row.get("data_source") or {}).get("event_tags", []),
                "event_tag_confidence": (row.get("data_source") or {}).get("event_tag_confidence"),
                "orderbook_status": (row.get("data_source") or {}).get("orderbook_status"),
                "end_date_bucket": (row.get("data_source") or {}).get("end_date_bucket"),
                "resolution_risk_status": (row.get("data_source") or {}).get("resolution_risk_status"),
            }
            for row in candidates[:15]
        ],
    }


def write_outputs(
    candidates: list[dict],
    observations: list[dict] | None = None,
    settings: dict | None = None,
    scan_metadata: dict | None = None,
) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "prediction_markets_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "mode": (settings or {}).get("mode", "paper"),
                "live_trading_allowed": bool((settings or {}).get("allow_live_trading", False)),
                "summary": summarize(candidates, scan_metadata=scan_metadata),
                "observations": observations or [],
                "candidates": candidates,
                "hard_limits": [
                    "Public prediction-market data only.",
                    "No credentials, account APIs, order APIs, or live trading.",
                    "Conditional prediction routes require account, API, and jurisdiction checks before live use.",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan public prediction markets.")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args(argv)
    from settings import load_settings

    batch = build_scan_batch(load_settings(), limit=args.top)
    candidates = batch.candidates
    path = RUNS_DIR / "prediction_markets_latest.json"
    for idx, row in enumerate(candidates[: args.top], start=1):
        print(f"{idx:>3} {row['score']:>6.1f} {row['venue']:<10} {row['direction']:<13} {row['last']:<6} {row['name'][:80]}")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
