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
import re
import urllib.parse
import urllib.request

from scan_batch import ScanBatch, observation_from_candidate
from strict_json_object import coerce_single_json_object


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
POLYMARKET_MARKET_CAP = 100


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


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _utc_datetime(value: object) -> dt.datetime | None:
    """Parse provider ISO, epoch-second, or epoch-millisecond timestamps."""
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None and math.isfinite(numeric):
        if abs(numeric) >= 10_000_000_000:
            numeric /= 1_000.0
        try:
            return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _freshness_fields(*values: object) -> tuple[str | None, float | None]:
    parsed = [timestamp for timestamp in (_utc_datetime(value) for value in values) if timestamp]
    if not parsed:
        return None, None
    # When both outcome books are present, the older timestamp is the safe age.
    timestamp = min(parsed)
    stale = max(0.0, (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds() / 60.0)
    return timestamp.isoformat(), round(stale, 3)


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
    end = _utc_datetime(value)
    if end is None:
        return "unknown"
    now = dt.datetime.now(dt.timezone.utc)
    days = (end - now).total_seconds() / 86400.0
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
        "sports": (
            "nba",
            "nfl",
            "mlb",
            "soccer",
            "football",
            "champion",
            "game",
            "world cup",
            "fifa",
            "uefa",
            "tournament",
            "premier league",
        ),
        "crypto": ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana"),
        "geopolitics": ("war", "ceasefire", "china", "taiwan", "ukraine", "israel"),
        "weather": ("hurricane", "temperature", "weather", "rain", "storm"),
        "regional": ("africa", "nigeria", "south africa", "brazil", "mexico", "argentina", "indonesia"),
    }.items():
        if any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) for term in terms):
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
    metadata = coerce_single_json_object(metadata, default={})
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
    if metadata.get("researcher_recommendation_status") == "rejected_incomplete":
        flags.append("incomplete_researcher_recommendation_rejected")
        flags.extend(
            str(item)
            for item in metadata.get("researcher_recommendation_errors", [])
            if isinstance(item, str) and item
        )
    return flags


def _days_to_end(end_date: object) -> float | None:
    end = _utc_datetime(end_date)
    if end is None:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    return (end - now).total_seconds() / 86400.0


def _has_required_research_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _validate_researcher_recommendation(payload: object) -> tuple[dict | None, list[str]]:
    recommendation = coerce_single_json_object(payload, default=None)
    if not isinstance(recommendation, dict) or not recommendation:
        return None, ["researcher_recommendation_not_single_json_object"]
    missing = [
        field
        for field in ("market_id", "recommended_action", "confidence", "thesis", "rationale")
        if not _has_required_research_value(recommendation.get(field))
    ]
    if missing:
        return None, [f"researcher_recommendation_missing_{field}" for field in missing]
    return recommendation, []


def _normalize_researcher_recommendation_metadata(metadata: object) -> dict:
    normalized = coerce_single_json_object(metadata, default={})
    if "researcher_recommendation" not in normalized:
        return normalized
    recommendation, errors = _validate_researcher_recommendation(normalized.get("researcher_recommendation"))
    if recommendation is None:
        normalized["researcher_recommendation_status"] = "rejected_incomplete"
        normalized["researcher_recommendation_errors"] = errors
        normalized.pop("researcher_recommendation_applied", None)
        return normalized
    normalized["researcher_recommendation"] = recommendation
    normalized["researcher_recommendation_status"] = "accepted"
    normalized["researcher_recommendation_errors"] = []
    return normalized


def _polymarket_paper_gate(candidate: dict, row: dict, settings: dict) -> tuple[bool, list[str]]:
    config = settings.get("prediction_market_scanner", {}) or {}
    if not config.get("polymarket_paper_gate_enabled", True):
        return True, []
    metadata = coerce_single_json_object(candidate.get("data_source"), default={})
    reasons = []
    days_to_end = _days_to_end(row.get("endDate") or metadata.get("endDate"))
    max_days = as_float(config.get("polymarket_max_days_to_resolution"), 30.0)
    min_liquidity = as_float(config.get("polymarket_min_liquidity_usd"), 1_000.0)
    max_spread_bps = as_float(config.get("polymarket_max_spread_bps"), 300.0)
    max_stale_minutes = as_float(config.get("polymarket_max_stale_minutes"), 0.0)
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
    stale_minutes = candidate.get("stale_minutes")
    if max_stale_minutes > 0:
        if stale_minutes is None:
            reasons.append("missing_freshness_timestamp")
        elif as_float(stale_minutes) > max_stale_minutes:
            reasons.append("stale_public_quote")
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
        best_bid_values = [
            as_float(item.get("price") if isinstance(item, dict) else item[0]) for item in bids
        ]
        best_bid_values = [value for value in best_bid_values if 0 < value < 1]
        best_bid = max(best_bid_values or [0.0])
        best_ask_values = [as_float(item.get("price") if isinstance(item, dict) else item[0]) for item in asks]
        best_ask_values = [value for value in best_ask_values if 0 < value < 1]
        best_ask = min(best_ask_values) if best_ask_values else 0.0
        spread = (best_ask - best_bid) * 10_000.0 if best_ask > best_bid > 0 else None
        return {
            "orderbook_status": "verified" if spread is not None else "empty",
            "orderbook_best_bid": round(best_bid, 6) if best_bid else None,
            "orderbook_best_ask": round(best_ask, 6) if best_ask else None,
            "orderbook_spread_bps": round(spread, 3) if spread is not None else None,
            "orderbook_depth_usd": round(_book_depth_from_levels(bids) + _book_depth_from_levels(asks), 3),
            "orderbook_timestamp": row.get("timestamp"),
            "orderbook_token_id": str(token_id),
            "orderbook_neg_risk": _as_bool(row.get("neg_risk")),
            "orderbook_source": "Polymarket CLOB public book",
        }
    except Exception as exc:  # noqa: BLE001
        return {"orderbook_status": "error", "orderbook_error": str(exc)[:160]}


def _polymarket_outcome_orderbooks(token_ids: list) -> dict:
    """Read and combine the public YES and NO books for one binary market."""
    yes_book = _polymarket_orderbook(token_ids[0] if len(token_ids) == 2 else None)
    no_book = _polymarket_orderbook(token_ids[1] if len(token_ids) == 2 else None)
    yes_status = yes_book.get("orderbook_status")
    no_status = no_book.get("orderbook_status")
    if yes_status == no_status == "verified":
        status = "verified"
    elif "error" in {yes_status, no_status}:
        status = "error"
    elif "missing_token" in {yes_status, no_status}:
        status = "missing_token"
    elif "empty" in {yes_status, no_status}:
        status = "empty"
    else:
        status = "partial"
    timestamp, stale_minutes = _freshness_fields(
        yes_book.get("orderbook_timestamp"),
        no_book.get("orderbook_timestamp"),
    )
    return {
        "orderbook_status": status,
        "orderbook_best_bid": yes_book.get("orderbook_best_bid"),
        "orderbook_best_ask": yes_book.get("orderbook_best_ask"),
        "orderbook_spread_bps": yes_book.get("orderbook_spread_bps"),
        "orderbook_depth_usd": round(
            as_float(yes_book.get("orderbook_depth_usd"))
            + as_float(no_book.get("orderbook_depth_usd")),
            3,
        ),
        "orderbook_timestamp": timestamp,
        "orderbook_stale_minutes": stale_minutes,
        "orderbook_neg_risk": bool(
            yes_book.get("orderbook_neg_risk") or no_book.get("orderbook_neg_risk")
        ),
        "yes_token_id": str(token_ids[0]) if len(token_ids) == 2 else None,
        "no_token_id": str(token_ids[1]) if len(token_ids) == 2 else None,
        "yes_best_bid": yes_book.get("orderbook_best_bid"),
        "yes_best_ask": yes_book.get("orderbook_best_ask"),
        "yes_spread_bps": yes_book.get("orderbook_spread_bps"),
        "yes_depth_usd": yes_book.get("orderbook_depth_usd"),
        "no_best_bid": no_book.get("orderbook_best_bid"),
        "no_best_ask": no_book.get("orderbook_best_ask"),
        "no_spread_bps": no_book.get("orderbook_spread_bps"),
        "no_depth_usd": no_book.get("orderbook_depth_usd"),
        "orderbook_source": "Polymarket CLOB public YES/NO books",
    }


def _kalshi_orderbook(ticker: object) -> dict:
    if not ticker:
        return {"orderbook_status": "missing_ticker"}
    try:
        row = fetch_json(
            f"https://external-api.kalshi.com/trade-api/v2/markets/{urllib.parse.quote(str(ticker))}/orderbook",
            timeout=8,
        )
        body = row.get("orderbook_fp") or row.get("orderbook") or row.get("market_orderbook") or row
        yes = body.get("yes_dollars") or body.get("yes") or body.get("yes_bids") or []
        no = body.get("no_dollars") or body.get("no") or body.get("no_bids") or []
        price_scale = 1.0 if body.get("yes_dollars") is not None or body.get("no_dollars") is not None else 0.01
        return {
            "orderbook_status": "verified" if yes or no else "empty",
            "orderbook_depth_usd": round(
                _book_depth_from_levels(yes, price_scale=price_scale)
                + _book_depth_from_levels(no, price_scale=price_scale),
                3,
            ),
            "orderbook_source": "Kalshi public market orderbook",
        }
    except Exception as exc:  # noqa: BLE001
        return {"orderbook_status": "error", "orderbook_error": str(exc)[:160]}


def feasibility(settings: dict) -> dict:
    return {
        "status": "conditional",
        "requires_short_spot": False,
        "paper_only": True,
        "public_data_only": True,
        "live_execution_supported": False,
        "legs": ["buy YES or NO event contract"],
        "notes": [
            "This adapter supports public market-data research only.",
            "No wallet, authenticated API, order placement, or live route is implemented.",
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
    metadata = _normalize_researcher_recommendation_metadata(metadata)
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


def _polymarket_reject_reason(row: object) -> str | None:
    """Keep v1 deliberately binary, open, unresolved, and CLOB-readable."""
    if not isinstance(row, dict):
        return "invalid_market_record"
    if "active" in row and not _as_bool(row.get("active")):
        return "inactive_market"
    if _as_bool(row.get("closed")) or _as_bool(row.get("resolved")) or _as_bool(row.get("archived")):
        return "closed_or_resolved"
    if "acceptingOrders" in row and not _as_bool(row.get("acceptingOrders")):
        return "orders_not_accepted"
    if _as_bool(row.get("negRisk")) or _as_bool(row.get("enableNegRisk")):
        return "ambiguous_multi_condition"
    if not row.get("id"):
        return "missing_market_id"
    if not (row.get("question") or row.get("title") or row.get("slug")):
        return "missing_title"
    expiry = row.get("endDate") or row.get("end_date")
    if not expiry:
        return "missing_expiry"
    if _end_date_bucket(expiry) == "expired_or_resolution_pending":
        return "expired_or_resolution_pending"
    outcomes = [str(value).strip().lower() for value in _json_list(row.get("outcomes"))]
    if outcomes and outcomes != ["yes", "no"]:
        return "ambiguous_outcomes"
    token_ids = _json_list(row.get("clobTokenIds") or row.get("clobTokenIDs"))
    if len(token_ids) != 2 or any(not str(token_id).strip() for token_id in token_ids):
        return "missing_binary_token_pair"
    prices = _json_list(row.get("outcomePrices"))
    if prices and len(prices) != 2:
        return "ambiguous_outcome_prices"
    if prices and any(not 0.0 <= as_float(price, -1.0) <= 1.0 for price in prices):
        return "invalid_outcome_prices"
    return None


def _polymarket_candidate_from_row(row: dict, settings: dict, orderbook: dict) -> dict | None:
    prices = _json_list(row.get("outcomePrices"))
    yes = as_float(prices[0] if prices else row.get("lastTradePrice"), 0.5)
    no = max(0.01, min(0.99, 1.0 - yes))
    one_week = as_float(row.get("oneWeekPriceChange"), 0.0) * 10_000.0
    direction = "buy_yes_event" if one_week >= 0 else "buy_no_event"
    token_ids = _json_list(row.get("clobTokenIds") or row.get("clobTokenIDs"))
    price = yes if direction == "buy_yes_event" else no
    spread_bps = as_float(row.get("spread"), 0.03) * 10_000.0
    yes_bid = as_float(orderbook.get("yes_best_bid"), 0.0)
    yes_ask = as_float(orderbook.get("yes_best_ask"), 0.0)
    if yes_ask > yes_bid > 0:
        yes = (yes_bid + yes_ask) / 2.0
        no = max(0.01, min(0.99, 1.0 - yes))
        price = yes if direction == "buy_yes_event" else no
        spread_bps = (yes_ask - yes_bid) * 10_000.0
    if orderbook.get("orderbook_timestamp"):
        freshness_timestamp, stale_minutes = _freshness_fields(orderbook.get("orderbook_timestamp"))
    else:
        freshness_timestamp, stale_minutes = _freshness_fields(
            row.get("updatedAt") or row.get("updated_at")
        )
    tag_details = _event_tag_details(row)
    title = row.get("question") or row.get("title") or row.get("slug") or "Polymarket market"
    expiry = row.get("endDate") or row.get("end_date")
    metadata = {
        "provider": "Polymarket Gamma API",
        "slug": row.get("slug"),
        "market_id": str(row.get("id")),
        "endDate": expiry,
        "outcome_timestamp_present": bool(expiry),
        "has_orderbook_token": len(token_ids) == 2,
        "event_tags": tag_details["tags"],
        "event_tag_confidence": tag_details["confidence"],
        "freshness_timestamp": freshness_timestamp,
        "stale_minutes": stale_minutes,
        **orderbook,
    }
    candidate = _candidate(
        "POLYMARKET",
        f"poly:{row.get('id')}",
        title,
        price,
        direction,
        as_float(row.get("liquidityNum") or row.get("liquidity")),
        as_float(row.get("volume24hr") or row.get("volume24hrClob")),
        spread_bps,
        one_week,
        settings,
        metadata,
    )
    candidate.update(
        {
            "market_id": str(row.get("id")),
            "title": str(title),
            "probability_mid": round(max(0.0, min(1.0, yes)), 6),
            "best_bid": round(yes_bid, 6) if yes_bid > 0 else None,
            "best_ask": round(yes_ask, 6) if yes_ask > 0 else None,
            "yes_best_bid": orderbook.get("yes_best_bid"),
            "yes_best_ask": orderbook.get("yes_best_ask"),
            "no_best_bid": orderbook.get("no_best_bid"),
            "no_best_ask": orderbook.get("no_best_ask"),
            "depth_usd": as_float(orderbook.get("orderbook_depth_usd")),
            "freshness_timestamp": freshness_timestamp,
            "stale_minutes": stale_minutes,
            "expiry": expiry,
            "paper_only": True,
            "read_only": True,
        }
    )
    return candidate


def _polymarket_candidates(settings: dict, limit: int) -> tuple[list[dict], dict]:
    scanner_cfg = settings.get("prediction_market_scanner", {}) or {}
    configured_cap = int(scanner_cfg.get("polymarket_market_cap", POLYMARKET_MARKET_CAP))
    market_cap = max(1, min(POLYMARKET_MARKET_CAP, configured_cap))
    requested_limit = max(1, min(int(limit), market_cap))
    url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode(
        {
            "active": "true",
            "closed": "false",
            "limit": str(requested_limit),
            "order": "liquidity",
            "ascending": "false",
        }
    )
    rows = fetch_json(url)
    if not isinstance(rows, list):
        raise ValueError("Polymarket public markets response must be a list")
    rows = sorted(
        rows,
        key=lambda row: as_float((row or {}).get("liquidityNum") or (row or {}).get("liquidity"))
        if isinstance(row, dict)
        else 0.0,
        reverse=True,
    )[:requested_limit]
    preliminary = []
    expired_filtered = 0
    reject_reasons: collections.Counter[str] = collections.Counter()
    enrich_top = max(0, min(requested_limit, int(scanner_cfg.get("orderbook_enrichment_top", 10))))
    paper_gate_filtered = 0
    paper_gate_reasons = collections.Counter()
    row_by_id = {}
    for row in rows:
        reject_reason = _polymarket_reject_reason(row)
        if reject_reason:
            reject_reasons[reject_reason] += 1
            if reject_reason == "expired_or_resolution_pending":
                expired_filtered += 1
            continue
        if _end_date_bucket(row.get("endDate") or row.get("end_date")) == "expired_or_resolution_pending":
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
            orderbook = _polymarket_outcome_orderbooks(token_ids)
            if orderbook.get("orderbook_neg_risk"):
                reject_reasons["ambiguous_multi_condition"] += 1
                continue
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
        "rejected_count": sum(reject_reasons.values()),
        "reject_reason_counts": dict(reject_reasons),
        "candidate_count": len(candidates),
        "market_cap": market_cap,
        "requested_limit": requested_limit,
        "catalog_order": "liquidity_desc",
        "orderbook_enrichment_top": enrich_top,
        "paper_gate_enabled": bool((settings.get("prediction_market_scanner", {}) or {}).get("polymarket_paper_gate_enabled", True)),
        "paper_gate_filtered_count": paper_gate_filtered,
        "paper_gate_reason_counts": dict(paper_gate_reasons),
    }


def polymarket_candidates(settings: dict, limit: int) -> list[dict]:
    candidates, _status = _polymarket_candidates(settings, limit)
    return candidates


def _kalshi_probability(row: dict, cents_key: str, dollars_key: str) -> float:
    if row.get(dollars_key) not in (None, ""):
        return as_float(row.get(dollars_key), 0.0)
    return as_float(row.get(cents_key), 0.0) / 100.0


def _kalshi_amount(row: dict, *keys: str) -> float:
    for key in keys:
        if row.get(key) not in (None, ""):
            return as_float(row.get(key), 0.0)
    return 0.0


def _kalshi_quote(row: dict) -> dict:
    yes_bid = _kalshi_probability(row, "yes_bid", "yes_bid_dollars")
    yes_ask = _kalshi_probability(row, "yes_ask", "yes_ask_dollars")
    no_bid = _kalshi_probability(row, "no_bid", "no_bid_dollars")
    no_ask = _kalshi_probability(row, "no_ask", "no_ask_dollars")
    last = _kalshi_probability(row, "last_price", "last_price_dollars")
    if last <= 0 and yes_bid > 0 and yes_ask > 0:
        last = (yes_bid + yes_ask) / 2.0
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last": last,
        "volume": _kalshi_amount(row, "volume_24h_fp", "volume_24h", "volume_fp", "volume"),
        "open_interest": _kalshi_amount(row, "open_interest_fp", "open_interest"),
        "liquidity": _kalshi_amount(row, "liquidity_dollars", "liquidity"),
    }


def _kalshi_reject_reason(row: dict, quote: dict) -> str | None:
    status = str(row.get("status") or row.get("settlement_status") or "").strip().lower()
    if status and status not in {"active", "open", "initialized"}:
        return "inactive_status"
    if not row.get("close_time"):
        return "missing_end_date"
    if quote["last"] <= 0:
        return "missing_price"
    has_two_sided_quote = quote["yes_bid"] > 0 and quote["yes_ask"] > 0
    if max(quote["volume"], quote["open_interest"], quote["liquidity"]) <= 0 and not has_two_sided_quote:
        return "zero_liquidity"
    return None


def _kalshi_candidate_from_row(row: dict, settings: dict, orderbook: dict) -> dict | None:
    close_time = row.get("close_time")
    quote = _kalshi_quote(row)
    if _kalshi_reject_reason(row, quote):
        return None
    yes_bid = quote["yes_bid"]
    yes_ask = quote["yes_ask"]
    no_bid = quote["no_bid"]
    no_ask = quote["no_ask"]
    last = quote["last"]
    spread_bps = (yes_ask - yes_bid) * 10_000.0 if yes_ask > yes_bid > 0 else 500.0
    volume = quote["volume"]
    open_interest = quote["open_interest"]
    liquidity = quote["liquidity"]
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
        "reported_liquidity": liquidity,
        "kalshi_price_schema": "dollars_fixed_point"
        if any(str(key).endswith(("_dollars", "_fp")) for key in row)
        else "legacy_cents",
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
        max(open_interest, liquidity),
        volume,
        spread_bps,
        as_float(row.get("price_delta_24h"), 0.0),
        settings,
        metadata,
    )


def _kalshi_candidates(settings: dict, limit: int) -> tuple[list[dict], dict]:
    scanner_cfg = settings.get("prediction_market_scanner", {})
    limit = min(int(limit), int(scanner_cfg.get("kalshi_market_cap", 50)))
    max_pages = max(1, min(5, int(scanner_cfg.get("kalshi_fetch_pages", 3))))
    page_size = max(100, limit)
    rows = []
    cursor = None
    fetched_pages = 0
    seen_tickers = set()
    for _page in range(max_pages):
        params = {"limit": str(page_size), "status": "open", "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = fetch_json("https://external-api.kalshi.com/trade-api/v2/markets?" + urllib.parse.urlencode(params))
        fetched_pages += 1
        for row in data.get("markets", []):
            ticker = str(row.get("ticker") or "")
            if ticker and ticker in seen_tickers:
                continue
            if ticker:
                seen_tickers.add(ticker)
            rows.append(row)
        cursor = data.get("cursor")
        accepted_quotes = sum(_kalshi_reject_reason(row, _kalshi_quote(row)) is None for row in rows)
        if not cursor or accepted_quotes >= limit:
            break
    preliminary = []
    expired_filtered = 0
    reject_reasons: collections.Counter[str] = collections.Counter()
    enrich_top = int(settings.get("prediction_market_scanner", {}).get("orderbook_enrichment_top", 10))
    row_by_id = {}
    for row in rows:
        close_time = row.get("close_time")
        if _end_date_bucket(close_time) == "expired_or_resolution_pending":
            expired_filtered += 1
            reject_reasons["expired_or_resolution_pending"] += 1
            continue
        quote = _kalshi_quote(row)
        reject_reason = _kalshi_reject_reason(row, quote)
        if reject_reason:
            reject_reasons[reject_reason] += 1
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
    preliminary = preliminary[:limit]
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
        "fetched_count": len(rows),
        "fetched_pages": fetched_pages,
        "expired_filtered_count": expired_filtered,
        "rejected_count": sum(reject_reasons.values()),
        "reject_reason_counts": dict(reject_reasons),
        "eligible_quote_count": len(preliminary),
        "candidate_count": len(candidates),
        "orderbook_enrichment_top": enrich_top,
        "multivariate_filter": "exclude",
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
    provider_reject_reasons: collections.Counter[str] = collections.Counter()
    for item in provider_status:
        provider_reject_reasons.update(item.get("reject_reason_counts") or {})
    return {
        "candidate_count": len(candidates),
        "expired_filtered_count": expired_filtered_count,
        "provider_status": provider_status,
        "provider_rejected_count": sum(provider_reject_reasons.values()),
        "provider_reject_reason_counts": dict(provider_reject_reasons),
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
                "mode": "paper",
                "live_trading_allowed": False,
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
