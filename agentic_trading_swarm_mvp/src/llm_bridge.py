"""LLM bridge for state reading and bounded recommendations.

This module does not call an LLM by itself. It creates a compact state packet
that LLM agents can read, and it ingests recommendations from a JSONL inbox into
safe internal artifacts: build tasks, growth experiments, and hunter directives.
"""

from __future__ import annotations

import hashlib
import json
import re
import pathlib
import sqlite3
from typing import Any

from storage import (
    RUNS_DIR,
    active_signal_policies,
    add_growth_experiment,
    add_hunter_directive,
    add_improvement_task,
    add_llm_recommendation,
    open_adapter_specs,
    open_experiments,
    open_hunter_directives,
    open_route_probe_tasks,
    open_self_improvement_experiments,
    open_tasks,
)
from code_evolution import code_evolution_summary, default_paper_recommendation
from self_improvement_open_pack import IMPLEMENTED_STATUS as OPEN_PACK_IMPLEMENTED_STATUS
from self_improvement_open_pack import is_duplicate_open_pack_text
from strategy_lab import strategy_lab_summary


STATE_JSON = RUNS_DIR / "llm_state_packet.json"
STATE_MD = RUNS_DIR / "llm_state_packet.md"
INBOX = RUNS_DIR / "llm_recommendations_inbox.jsonl"
PROCESSED = RUNS_DIR / "llm_recommendations_processed.jsonl"

IMPLEMENTED_MANUAL_STATUSES = {
    "route_requirements": (
        "implemented_route_requirements",
        ("improvement_tasks", "route_probe_tasks"),
    ),
    "frontier_crypto_adapter": (
        "implemented_frontier_crypto_adapter",
        ("improvement_tasks", "adapter_specs"),
    ),
    "failure_diagnostics": (
        "implemented_failure_diagnostics",
        ("improvement_tasks", "adapter_specs"),
    ),
    "signal_redesign": (
        "implemented_signal_redesign",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_data_quality": (
        "implemented_frontier_data_quality",
        ("improvement_tasks", "adapter_specs"),
    ),
    "okx_basis_signal_research": (
        "implemented_okx_basis_signal_research",
        ("adapter_specs",),
    ),
    "regional_frontier_data": (
        "implemented_regional_frontier_data",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_systemic_redesign": (
        "implemented_frontier_systemic_redesign",
        ("improvement_tasks", "growth_experiments"),
    ),
    "okx_reliable_outcomes": (
        "implemented_okx_reliable_outcomes",
        ("improvement_tasks",),
    ),
    "strategy_reliability_pack": (
        "implemented_strategy_reliability_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "self_improvement_open_pack": (
        OPEN_PACK_IMPLEMENTED_STATUS,
        ("improvement_tasks", "growth_experiments"),
    ),
    "regional_fx_frontier_prediction_pack": (
        "implemented_regional_fx_frontier_prediction_pack",
        ("route_probe_tasks", "improvement_tasks", "adapter_specs"),
    ),
    "bybit_quality_decay_expansion_pack": (
        "implemented_bybit_quality_decay_expansion_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "kucoin_long_repair_diagnostics": (
        "implemented_kucoin_long_repair_diagnostics",
        ("improvement_tasks", "growth_experiments"),
    ),
    "global_market_discovery_scan": (
        "implemented_global_market_discovery_scan",
        ("adapter_specs", "growth_experiments", "route_probe_tasks", "market_hunter_directives"),
    ),
}

GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS = (
    "proxy map",
    "proxy-priced",
    "bybit",
    "bitso",
    "valr",
    "luno",
    "b3",
    "cme group",
    "ftse/jse top 40",
    "ftse/jse all share",
    "eurex",
    "national stock exchange of india",
    "japan exchange group",
    "frankfurter",
    "ecb reference fx",
    "manifold markets",
    "finra trace",
    "pinnacle api",
    "london stock exchange",
    "tmx group",
    "hong kong exchanges",
    "euronext",
    "taiwan stock exchange",
    "korea exchange",
    "bolsa mexicana",
    "australian securities exchange",
    "six swiss exchange",
    "cboe global markets",
    "nifty 50",
    "nifty next 50",
    "johannesburg stock exchange",
    "singapore exchange",
    "intercontinental exchange",
    "saudi exchange",
    "london metal exchange",
)

REQUIRED_RECOMMENDATION_FIELDS = (
    "action",
    "priority",
    "title",
    "rationale",
    "market_key",
    "evidence",
    "proposed_change",
)

MARKET_SCOUT_FALLBACK_RECOMMENDATION = {
    "action": "hold",
    "priority": 50,
    "title": "Fallback paper recommendation",
    "rationale": "Auto-generated because the primary response failed schema validation.",
    "market_key": "paper_system.integrity.market_scout",
    "evidence": {
        "issue": "schema_validation_failed",
    },
    "proposed_change": {"goal": "preserve parser compatibility"},
}

EXECUTION_ROUTE_HUNTER_FALLBACK_RECOMMENDATION = default_paper_recommendation(
    {
        "action": "refine",
        "priority": 85,
        "title": "Refine paper execution route recommendation",
        "rationale": (
            "Auto-generated because the primary execution-route response failed strict "
            "single-object validation, omitted required fields, or did not provide "
            "enough paper-only evidence to support routing analysis."
        ),
        "market_key": "paper.execution_route_hunter",
        "evidence": {
            "issue": "route_validation_failed",
            "validation_error": "schema_validation_failed",
            "paper_only": True,
        },
        "proposed_change": {
            "summary": "Return one schema-complete paper-only route recommendation or a conservative hold/refine decision.",
            "fallback_behavior": "refine_or_hold_with_validation_evidence",
            "required_fields": ", ".join(REQUIRED_RECOMMENDATION_FIELDS),
            "safety_mode": "paper_only",
            "suppress_live_execution_wording": True,
        },
    }
)

CODE_CHANGE_ACTIONABLE_FIELDS = (
    "change_category",
    "implementation_mode",
    "expected_files",
    "tests_to_run",
    "rollback_criteria",
)

CODE_CHANGE_OPTIONAL_DETAIL_FIELDS = (
    "summary",
    "expected_effect",
    "validation",
)

KNOWN_QUOTE_ASSETS = (
    "USDT",
    "USDC",
    "USDE",
    "DAI",
    "FDUSD",
    "TUSD",
    "BUSD",
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "AUD",
    "CAD",
    "CHF",
    "BRL",
    "MXN",
    "ZAR",
    "NGN",
    "IDR",
    "MYR",
    "SGD",
    "HKD",
    "TRY",
    "AED",
    "INR",
    "KRW",
    "BTC",
    "ETH",
)

INSTRUMENT_ID_KEYS = (
    "instId",
    "inst_id",
    "instrument_id",
    "market_symbol",
    "perp_instId",
    "spot_instId",
)

SYMBOL_KEYS = ("symbol", "ticker")

LEG_CONTAINER_KEYS = ("legs", "leg_metadata")

LEG_VALUE_KEYS = ("perp_leg", "spot_leg", "long_leg", "short_leg", "buy_leg", "sell_leg")

CANONICAL_MARKET_ID_KEYS = (
    "canonical_venue",
    "canonical_underlying",
    "canonical_instrument_id",
    "contract_context",
    "instrument_type",
    "tenor",
    "expiry",
    "context_key",
    "context_key_alias",
    "venue_family",
    "market_family",
    "scanner_family",
    "direction_family",
    "carry_profile",
    "execution_family",
    "canonical_market_identity",
)


def _signal_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        order by score_adjustment desc, closed_count desc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _contextual_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select context_key, closed_count, wins, avg_pnl_bps, win_rate, updated_at
        from contextual_stats
        order by closed_count desc, abs(avg_pnl_bps) desc
        limit 50
        """
    ).fetchall()
    stats: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["context_key_alias"] = _normalized_context_key(item.get("context_key"))
        item.update(
            _bounded_context_prior(
                item.get("closed_count"),
                item.get("win_rate"),
                item.get("avg_pnl_bps"),
            )
        )
        stats.append(item)
    return stats


def _crypto_venue_health_gaps(items: list[dict]) -> list[dict]:
    gaps = []
    for item in items or []:
        venue = str(item.get("venue") or "").lower()
        status = str(item.get("status") or "")
        response_status = item.get("response_status")
        route_id = str(item.get("route_id") or "")
        url = str(item.get("url") or "")
        adapter_trace = item.get("adapter_trace")
        failure_status = " ".join(part for part in (status, str(response_status or "")) if part).strip()
        if venue != "bybit" or "403" not in failure_status:
            continue
        linear_hint = "category=linear" in url or "linear" in route_id or "perp" in route_id
        if not linear_hint:
            continue
        if response_status is None and "403" in status:
            response_status = 403
        gap = {
            "venue": item.get("venue"),
            "route_id": item.get("route_id"),
            "asset": item.get("asset"),
            "status": item.get("status"),
            "response_status": response_status,
            "fallback_route_id": "bybit_spot_public",
            "fallback_endpoints": [
                "/v5/market/tickers?category=spot&symbol=BTCUSDT",
                "/v5/market/orderbook?category=spot&symbol=BTCUSDT",
            ],
            "paper_only_use": "scanner_inputs_and_venue_health",
            "rationale": "Bybit linear public reads returned 403; keep paper observability alive with spot public ticker/book endpoints.",
            "adapter_fix": {
                "method": "GET",
                "path": "/v5/market/tickers",
                "query": {
                    "category": "spot",
                    "symbol": "BTCUSDT",
                },
                "headers": {
                    "Accept": "application/json",
                    "User-Agent": "paper-research",
                },
                "response_fields": [
                    "result.list[0].lastPrice",
                    "result.list[0].bid1Price",
                    "result.list[0].ask1Price",
                ],
            },
        }
        if adapter_trace not in (None, "", [], {}):
            gap["adapter_trace"] = adapter_trace
        elif url:
            gap["adapter_trace"] = {"observed_url": url}
        gaps.append(gap)
    return gaps[:10]


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_context_fragment(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:48] if text else "unknown"


def _joined_context_text(item: dict[str, Any], *keys: str) -> str:
    values: list[str] = []
    for source in _identity_sources(item):
        for key in keys:
            value = source.get(key)
            if _is_missing_text(value):
                continue
            values.append(str(value).strip().lower())
    return " | ".join(dict.fromkeys(values))


def _normalized_venue_family(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if "okx" in text:
        return "okx"
    if "binance" in text:
        return "binance"
    if "bybit" in text:
        return "bybit"
    if "coinbase" in text:
        return "coinbase"
    if "kraken" in text:
        return "kraken"
    return _normalized_context_fragment(text)


def _normalized_context_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    parts = [part.strip() for part in re.split(r"[|/,:]+", text) if part.strip()]
    if len(parts) >= 6:
        return "|".join(
            [
                _normalized_context_fragment(parts[0]),
                _normalized_venue_family(parts[1]),
                _normalized_context_fragment(parts[2]),
                _normalized_context_fragment(parts[3]),
                _normalized_context_fragment(parts[4]),
                _normalized_context_fragment(parts[5]),
            ]
        )
    return _normalized_context_fragment(text)


def _bounded_context_prior(closed_count: Any, win_rate: Any, avg_pnl_bps: Any) -> dict[str, Any]:
    count = _safe_float(closed_count) or 0.0
    if count < 5:
        return {"prior_score": 0.0, "prior_confidence": 0.0}
    wr = max(0.0, min(1.0, _safe_float(win_rate) or 0.0))
    pnl = _safe_float(avg_pnl_bps) or 0.0
    score = max(-1.0, min(1.0, ((wr - 0.5) * 2.0) + (pnl / 500.0)))
    confidence = max(0.0, min(1.0, count / (count + 10.0)))
    return {"prior_score": score, "prior_confidence": confidence}

def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _is_missing_text(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or text.lower() in {"unknown", "n/a", "na", "none", "null"}


def _normalized_asset_token(value: Any) -> str | None:
    if _is_missing_text(value):
        return None
    token = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if len(token) < 2 or len(token) > 20:
        return None
    return token


def _candidate_venue(item: dict[str, Any]) -> str:
    for key in ("venue", "venue_or_source", "source", "exchange"):
        value = item.get(key)
        if not _is_missing_text(value):
            return str(value).strip().lower()
    return ""


def _normalized_candidate_pair(
    base_asset: Any,
    quote_asset: Any,
    venue: str,
    parse_confidence: str,
    *,
    require_known_quote: bool,
) -> dict[str, Any] | None:
    base = _normalized_asset_token(base_asset)
    quote = _normalized_asset_token(quote_asset)
    if not base or not quote:
        return None
    if require_known_quote and quote not in KNOWN_QUOTE_ASSETS:
        return None
    symbol = f"{base}/{quote}"
    return {
        "base_asset": base,
        "quote_asset": quote,
        "normalized_instrument_id": f"{base}-{quote}",
        "normalized_symbol": symbol,
        "asset_key": f"{venue}:{symbol}" if venue else symbol,
        "parse_confidence": parse_confidence,
    }


def _pair_from_hyphenated_symbol(value: Any, venue: str, parse_confidence: str) -> dict[str, Any] | None:
    text = str(value or "").strip().upper()
    if not text or not any(separator in text for separator in ("-", "/", "_", ":")):
        return None
    parts = [part for part in re.split(r"[-/_:]", text) if part]
    if len(parts) < 2:
        return None
    return _normalized_candidate_pair(
        parts[0],
        parts[1],
        venue,
        parse_confidence,
        require_known_quote=True,
    )


def _pair_from_concatenated_symbol(value: Any, venue: str, parse_confidence: str) -> dict[str, Any] | None:
    raw = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if len(raw) < 5:
        return None
    for quote in sorted(KNOWN_QUOTE_ASSETS, key=len, reverse=True):
        if not raw.endswith(quote) or len(raw) <= len(quote):
            continue
        base = raw[: -len(quote)]
        parsed = _normalized_candidate_pair(
            base,
            quote,
            venue,
            parse_confidence,
            require_known_quote=True,
        )
        if parsed:
            return parsed
    return None


def _iter_candidate_legs(item: dict[str, Any]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for key in LEG_CONTAINER_KEYS:
        value = item.get(key)
        if isinstance(value, list):
            legs.extend(leg for leg in value if isinstance(leg, dict))
    for key in LEG_VALUE_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            legs.append(value)
    return legs


def _pair_from_explicit_fields(item: dict[str, Any], venue: str, parse_confidence: str) -> dict[str, Any] | None:
    for base_key in ("base_asset", "baseAsset", "baseCcy", "base_currency"):
        for quote_key in ("quote_asset", "quoteAsset", "quoteCcy", "quote_currency"):
            parsed = _normalized_candidate_pair(
                item.get(base_key),
                item.get(quote_key),
                venue,
                parse_confidence,
                require_known_quote=False,
            )
            if parsed:
                return parsed
    return None


def _parse_candidate_assets(item: dict[str, Any]) -> dict[str, Any] | None:
    venue = _candidate_venue(item)
    explicit = _pair_from_explicit_fields(item, venue, "explicit")
    if explicit:
        return explicit
    for key in INSTRUMENT_ID_KEYS:
        parsed = _pair_from_hyphenated_symbol(item.get(key), venue, "inst_id")
        if parsed:
            return parsed
    for key in SYMBOL_KEYS:
        parsed = _pair_from_hyphenated_symbol(item.get(key), venue, "symbol")
        if parsed:
            return parsed
        parsed = _pair_from_concatenated_symbol(item.get(key), venue, "concatenated_symbol")
        if parsed:
            return parsed
    for leg in _iter_candidate_legs(item):
        parsed = _pair_from_explicit_fields(leg, venue, "legs")
        if parsed:
            return parsed
        for key in INSTRUMENT_ID_KEYS:
            parsed = _pair_from_hyphenated_symbol(leg.get(key), venue, "legs")
            if parsed:
                return parsed
        for key in SYMBOL_KEYS:
            parsed = _pair_from_hyphenated_symbol(leg.get(key), venue, "legs")
            if parsed:
                return parsed
            parsed = _pair_from_concatenated_symbol(leg.get(key), venue, "legs")
            if parsed:
                return parsed
    return None


def _identity_sources(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [item, *_iter_candidate_legs(item)]


def _first_present_text(item: dict[str, Any], *keys: str) -> str | None:
    for source in _identity_sources(item):
        for key in keys:
            value = source.get(key)
            if not _is_missing_text(value):
                return str(value).strip()
    return None


def _contract_context_from_instrument_id(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    parts = [part for part in re.split(r"[-/_:]", text) if part]
    if len(parts) < 3:
        return None
    tail = parts[2:]
    if not tail:
        return None
    has_contract_hint = any(
        part in {"SWAP", "PERP", "FUTURE", "FUTURES", "THISWEEK", "NEXTWEEK", "QUARTER", "NEXTQUARTER"}
        or re.fullmatch(r"\d{6,8}", part)
        for part in tail
    )
    if not has_contract_hint:
        return None
    return "-".join(tail)


def _normalized_market_identity(item: dict[str, Any]) -> dict[str, Any]:
    venue = _first_present_text(item, "canonical_venue") or _candidate_venue(item) or None
    base_asset = _normalized_asset_token(
        _first_present_text(item, "base_asset", "baseAsset", "baseCcy", "base_currency")
    )
    quote_asset = _normalized_asset_token(
        _first_present_text(item, "quote_asset", "quoteAsset", "quoteCcy", "quote_currency")
    )
    canonical_underlying = _normalized_asset_token(
        _first_present_text(item, "canonical_underlying", "underlying", "underlying_asset", "uly")
    ) or base_asset
    canonical_instrument_id = _first_present_text(
        item,
        "canonical_instrument_id",
        "normalized_instrument_id",
        "instrument_id",
        "instId",
        "inst_id",
        "market_symbol",
    )
    contract_context = _first_present_text(
        item,
        "contract_context",
        "contract",
        "contract_code",
        "contract_family",
        "tenor",
        "expiry",
        "expiry_date",
        "maturity_date",
        "settlement_date",
    ) or _contract_context_from_instrument_id(canonical_instrument_id)
    instrument_type = _first_present_text(item, "instrument_type", "instType")
    normalized_pair = _normalized_candidate_pair(
        base_asset,
        quote_asset,
        str(venue or "").lower(),
        "identity",
        require_known_quote=False,
    )
    fields: dict[str, Any] = {}
    if venue:
        fields["canonical_venue"] = str(venue).strip().lower()
    if canonical_underlying:
        fields["canonical_underlying"] = canonical_underlying
    if canonical_instrument_id:
        fields["canonical_instrument_id"] = str(canonical_instrument_id).strip().upper()
    if contract_context:
        fields["contract_context"] = str(contract_context).strip().upper()
    if instrument_type:
        fields["instrument_type"] = str(instrument_type).strip().upper()
    if normalized_pair:
        fields.update(normalized_pair)
    symbol_or_underlying = fields.get("normalized_symbol") or canonical_underlying
    disambiguator = fields.get("contract_context") or fields.get("instrument_type") or fields.get("canonical_instrument_id")
    if fields.get("canonical_venue") and symbol_or_underlying and disambiguator:
        fields["canonical_market_identity"] = f"{fields['canonical_venue']}:{symbol_or_underlying}:{disambiguator}"
    return fields


def _extract_candidate_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for key in ("candidates", "top_candidates", "normalized_candidates", "recent_candidates"):
        value = report.get(key)
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                enriched = dict(item)
                parsed = _parse_candidate_assets(enriched)
                if parsed:
                    for field in ("base_asset", "quote_asset", "normalized_instrument_id", "normalized_symbol", "asset_key"):
                        if _is_missing_text(enriched.get(field)):
                            enriched[field] = parsed[field]
                    if _is_missing_text(enriched.get("parse_confidence")):
                        enriched["parse_confidence"] = parsed["parse_confidence"]
                else:
                    if _is_missing_text(enriched.get("parse_confidence")):
                        enriched["parse_confidence"] = "unparsed"
                normalized_existing = _normalized_candidate_pair(
                    enriched.get("base_asset"),
                    enriched.get("quote_asset"),
                    _candidate_venue(enriched),
                    str(enriched.get("parse_confidence") or "explicit"),
                    require_known_quote=False,
                )
                if normalized_existing:
                    enriched["base_asset"] = normalized_existing["base_asset"]
                    enriched["quote_asset"] = normalized_existing["quote_asset"]
                    if _is_missing_text(enriched.get("normalized_instrument_id")):
                        enriched["normalized_instrument_id"] = normalized_existing["normalized_instrument_id"]
                    if _is_missing_text(enriched.get("normalized_symbol")):
                        enriched["normalized_symbol"] = normalized_existing["normalized_symbol"]
                    if _is_missing_text(enriched.get("asset_key")):
                        enriched["asset_key"] = normalized_existing["asset_key"]
                identity_fields = _normalized_market_identity(enriched)
                for field, value in identity_fields.items():
                    if _is_missing_text(enriched.get(field)):
                        enriched[field] = value
                candidates.append(enriched)
    return candidates


def _infer_route_feasibility_state(item: dict[str, Any]) -> str:
    explicit_state = str(item.get("route_feasibility_state") or "").strip().lower()
    if explicit_state in {"standard", "conditional", "blocked"}:
        return explicit_state
    blockers = {text.lower() for text in _as_text_list(item.get("route_blockers") or item.get("blockers"))}
    access = str(item.get("data_access_type") or "").strip().lower()
    tradability = str(item.get("tradability_guess") or "").strip().lower()
    next_action = str(item.get("recommended_next_action") or "").strip().lower()
    if access in {"broker_account", "paid_data"} or next_action == "ignore":
        return "blocked"
    if access == "public_no_key" and tradability == "directly_tradable" and not blockers:
        return "standard"
    if blockers or tradability in {"route_needed", "watch_only"} or access in {"public_key_required", "unknown"}:
        return "conditional"
    return "blocked"


def _compact_frontier_execution_quality(research_worker: dict[str, Any] | None) -> dict[str, Any]:
    report = research_worker if isinstance(research_worker, dict) else {}
    review = report.get("execution_quality_review")
    if not isinstance(review, dict):
        review = report.get("frontier_execution_quality")
    review = review if isinstance(review, dict) else {}
    candidates = _extract_candidate_rows(report)
    quote_age_ms_max = _safe_float(review.get("quote_age_ms_max")) or 15000.0
    normalized_spread_bps_max = _safe_float(review.get("normalized_spread_bps_max")) or 25.0
    short_frontier_spot_spread_bps_max = (
        _safe_float(review.get("normalized_spread_bps_max_short_frontier_spot"))
        or min(normalized_spread_bps_max, 18.0)
    )
    minimum_depth_notional = _safe_float(review.get("minimum_depth_notional")) or 1000.0
    hold_on_missing_metrics = bool(review.get("hold_on_missing_metrics", True))
    route_counts = {"standard": 0, "conditional": 0, "blocked": 0}
    failing_gate_counts = {
        "route_feasibility_state": 0,
        "quote_age_ms": 0,
        "normalized_spread_bps": 0,
        "top_of_book_depth_notional": 0,
        "missing_quote_metrics": 0,
    }
    priority_watchlist: list[dict[str, Any]] = []
    hold_candidate_count = 0
    for item in candidates:
        route_state = _infer_route_feasibility_state(item)
        route_counts[route_state] = route_counts.get(route_state, 0) + 1
        surface_type = str(item.get("surface_type_raw") or item.get("surface_type") or "")
        direction = str(item.get("side") or item.get("direction") or "")
        quote_age_ms = _safe_float(item.get("quote_age_ms"))
        spread_bps = _safe_float(item.get("normalized_spread_bps"))
        depth_notional = (
            _safe_float(item.get("top_of_book_depth_notional"))
            or _safe_float(item.get("best_bid_ask_depth_notional"))
            or _safe_float(item.get("depth_notional"))
        )
        required_notional = _safe_float(item.get("required_paper_notional")) or minimum_depth_notional
        spread_limit = normalized_spread_bps_max
        if direction.strip().lower() == "short" and "spot" in surface_type.lower():
            spread_limit = short_frontier_spot_spread_bps_max
        failed_gates: list[str] = []
        if route_state != "standard":
            failing_gate_counts["route_feasibility_state"] += 1
            failed_gates.append("route_feasibility_state")
        missing_metrics = quote_age_ms is None or spread_bps is None or depth_notional is None
        if quote_age_ms is not None and quote_age_ms > quote_age_ms_max:
            failing_gate_counts["quote_age_ms"] += 1
            failed_gates.append("quote_age_ms")
        if spread_bps is not None and spread_bps > spread_limit:
            failing_gate_counts["normalized_spread_bps"] += 1
            failed_gates.append("normalized_spread_bps")
        if depth_notional is not None and depth_notional < required_notional:
            failing_gate_counts["top_of_book_depth_notional"] += 1
            failed_gates.append("top_of_book_depth_notional")
        if missing_metrics and hold_on_missing_metrics:
            failing_gate_counts["missing_quote_metrics"] += 1
            failed_gates.append("missing_quote_metrics")
        if failed_gates:
            hold_candidate_count += 1
            if len(priority_watchlist) < 10:
                priority_watchlist.append(
                    {
                        "venue_or_source": item.get("venue_or_source"),
                        "asset_or_event": item.get("asset_or_event"),
                        "surface_type_raw": surface_type,
                        "route_feasibility_state": route_state,
                        "failed_gates": failed_gates,
                        "route_blockers": _as_text_list(item.get("route_blockers") or item.get("blockers")),
                    }
                )
    return {
        "paper_only": True,
        "admission_gate": {
            "quote_age_ms_max": int(quote_age_ms_max),
            "normalized_spread_bps_max": normalized_spread_bps_max,
            "normalized_spread_bps_max_short_frontier_spot": short_frontier_spot_spread_bps_max,
            "minimum_depth_notional": minimum_depth_notional,
            "require_route_feasibility_state": "standard",
            "hold_on_missing_metrics": hold_on_missing_metrics,
        },
        "candidate_count": len(candidates),
        "route_feasibility_counts": route_counts,
        "hold_candidate_count": hold_candidate_count,
        "failing_gate_counts": failing_gate_counts,
        "priority_watchlist": priority_watchlist,
    }


def _bucketize(stats: list[dict], directives: list[dict]) -> dict:
    buckets = {"exploit": [], "explore": [], "diagnose": []}

    for item in stats:
        if item["score_adjustment"] > 2 and item["closed_count"] >= 3:
            buckets["exploit"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "positive learned score adjustment",
                    "evidence": item,
                }
            )
        elif item["closed_count"] < 5:
            buckets["explore"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "insufficient evidence; preserve exploration",
                    "evidence": item,
                }
            )
        elif item["score_adjustment"] < -2 or item["avg_pnl_bps"] < 0:
            buckets["diagnose"].append(
                {
                    "signal_key": item["signal_key"],
                    "reason": "negative performance or learned penalty",
                    "evidence": item,
                }
            )

    for directive in directives:
        if directive["directive"] == "exploit_more":
            buckets["exploit"].append(directive)
        elif directive["directive"] in {"observe", "expand_route_resolver", "route_resolver_active", "collect_market_hours_data"}:
            buckets["explore"].append(directive)
        elif directive["directive"] in {"demote_or_filter", "decay_watch", "red_team", "market_decay_watch"}:
            buckets["diagnose"].append(directive)

    return buckets


def _recommendation_schema(allowed_actions: list[str]) -> dict:
    required_fields = list(REQUIRED_RECOMMENDATION_FIELDS)
    required_fields_csv = ", ".join(REQUIRED_RECOMMENDATION_FIELDS)
    return {
        "action": "one allowed action",
        "required_fields": required_fields,
        "required_fields_csv": required_fields_csv,
        "response_contract": "Return exactly one top-level JSON object and nothing else.",
        "top_level_shape": "A single JSON object is required; top-level arrays are invalid.",
        "format_guardrails": "No markdown, commentary, code fences, or wrapper arrays around the recommendation object.",
        "final_serialization_guard": (
            "Before returning, build the full recommendation object, verify action, "
            "priority, title, rationale, market_key, evidence, and proposed_change are "
            "all present and non-empty, confirm no field value is an array, serialize the "
            "object as valid JSON, and return only that serialized object."
        ),
        "paper_only_default": (
            "Recommendations must remain limited to paper-trading simulation, reports, "
            "tests, adapters, routing analysis, and code evolution. market_key must stay "
            "paper-scoped and must not imply live execution."
        ),
        "priority": "integer 1-100",
        "fallback_behavior": (
            "If any required field is unavailable, route construction fails validation, "
            "or the response would otherwise be partial, emit one conservative paper-only "
            "fallback recommendation instead of partial output. The fallback must also be "
            "returned as exactly one JSON object."
        ),
        "validation_policy": {
            "publish_only_single_json_object": True,
            "reject_non_json": True,
            "reject_wrapper_arrays": True,
            "reject_array_values_anywhere": True,
            "reject_missing_required_fields": True,
            "required_serialization_fields": required_fields,
            "required_fields": required_fields,
            "required_fields_csv": required_fields_csv,
            "require_explicit_paper_only_scope": True,
            "paper_execution_route_hunter_fallback": "refine_or_hold_with_validation_evidence",
            "require_non_empty_market_key": True,
            "require_non_empty_rationale": True,
            "require_no_extra_text_outside_object": True,
            "serialize_then_return_single_object": True,
            "require_non_empty_evidence_object": True,
            "require_non_empty_proposed_change_object": True,
            "require_priority_integer_range": [1, 100],
        },
        "title": "short directive",
        "rationale": "non-empty explanation of why this matters",
        "signal_key": "optional",
        "market_key": "required stable routing key",
        "evidence": "non-empty object with concrete supporting facts, validation details, or observed failures",
        "proposed_change": "non-empty object describing what should be built/tested/researched",
        "allocation_update_gate": (
            "If market_key, rationale, evidence, or proposed_change would be blank, partial, "
            "or non-JSON, emit a conservative hold/refine recommendation instead of suggesting a paper allocation change."
        ),
        "variant_config": "required only for propose_signal_variant; bounded frontier variant object",
        "strategy_lab_experiment": {
            "required_only_for": "propose_strategy_lab_experiment",
            "purpose": "Invent a new paper-only strategy idea that can be tested through the existing candidate/review/paper engine.",
            "required_fields": [
                "strategy_lab_id",
                "hypothesis",
                "strategy_logic",
                "data_requirements",
                "risk_gates",
                "promotion_rules",
            ],
            "strategy_logic_contract": (
                "Use type='candidate_filter' for v1. Supported filters include venues, trade_types, "
                "directions, regions, asset_classes, min_edge_bps, min_score, min_liquidity_score, "
                "max_spread_bps, min_quality_score, max_stale_minutes, and required_fields."
            ),
            "paper_only_rule": "This creates experimental paper candidates only; deterministic outcomes decide promotion.",
        },
        "code_change": {
            "required_only_for": "propose_code_change",
            "required_actionable_fields": list(CODE_CHANGE_ACTIONABLE_FIELDS),
            "change_category": "one allowed code-evolution category",
            "required_actionable_fields_csv": ", ".join(CODE_CHANGE_ACTIONABLE_FIELDS),
            "implementation_mode": "runtime_active, paper_policy, shadow_trial, or report_only",
            "expected_files": "list of repo-relative files expected to change",
            "tests_to_run": "safe unittest commands or empty list for full regression",
            "rollback_criteria": "when the governor should revert/demote",
            "unified_diff": "optional patch; if missing, GPT-5.5 Build Planner may generate one",
            "detail_fields_to_prefer": list(CODE_CHANGE_OPTIONAL_DETAIL_FIELDS),
            "field_quality_gate": (
                "Arrays are forbidden in the returned recommendation object; stringify field sets when needed. "
                "Populate every required actionable field inside code_change. Sparse "
                "code_change objects are downgraded because Build Planner needs category, "
                "implementation mode, expected files, tests, and rollback criteria."
            ),
            "detail_placement_rule": (
                "If implementation details such as summary, expected effect, validation, "
                "ingestion, normalization, or scanner_logic are known, place them under "
                "code_change instead of only at the top level."
            ),
            "mirror_top_level_when_nested_omitted": (
                "If compatibility requires repeating implementation details at the top "
                "level, duplicate the same values under code_change so the proposal is "
                "not downgraded as missing actionable code-change fields."
            ),
            "partial_output_policy": (
                "If a safe code change cannot be described with these fields, emit one "
                "conservative paper-only hold/refine recommendation instead of a thin "
                "propose_code_change payload."
            ),
            "frontier_escalation_reason": "required for GPT-5.5 code evolution",
        },
        "market_key_contracts": {
            "paper.execution_route_hunter": (
                "Always emit exactly one schema-complete top-level JSON object with "
                "action, priority, title, rationale, market_key, evidence, and "
                "proposed_change. No markdown, commentary, wrapper arrays, or live "
                "execution wording. If route construction fails validation or context "
                "is incomplete, emit the provided paper-only refine/hold fallback "
                "recommendation object with the validation failure captured in evidence."
            ),
            "paper_system.integrity.market_scout": "Always emit exactly one schema-complete top-level JSON object with action, priority, title, rationale, market_key, evidence, and proposed_change. If generation or validation fails, emit the provided fallback paper-only hold recommendation object instead of partial output.",
        },
        "paper_safety_policies": {
            "paper.execution_route_hunter": {
                "mode": "paper_only",
                "forbid_live_execution_wording": True,
                "required_fields": required_fields,
            },
        },
        "fallback_recommendations": {"paper.execution_route_hunter": EXECUTION_ROUTE_HUNTER_FALLBACK_RECOMMENDATION, "paper_system.integrity.market_scout": MARKET_SCOUT_FALLBACK_RECOMMENDATION},
        "allowed_actions": allowed_actions,
    }


def write_llm_state_packet(conn: sqlite3.Connection, payload: dict, settings: dict) -> dict:
    if not settings.get("llm_bridge", {}).get("enabled", True):
        return {}

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stats = _signal_stats(conn)
    contextual_stats = _contextual_stats(conn)
    directives = open_hunter_directives(conn)
    tasks = open_tasks(conn)
    experiments = open_experiments(conn)
    policies = [_compact_policy(item) for item in active_signal_policies(conn)]
    self_improvement_experiments = [_compact_experiment(item) for item in open_self_improvement_experiments(conn, limit=50)]
    route_probe_tasks = open_route_probe_tasks(conn, limit=30)
    adapter_specs = open_adapter_specs(conn, limit=30)
    buckets = _bucketize(stats, directives)
    global_market_discovery = _compact_global_market_discovery(payload.get("research_worker"))
    hunter_allocation = payload.get("hunter_allocation", {})
    allowed_actions = settings.get("llm_bridge", {}).get("allowed_actions", [])
    crypto_venue_health = payload.get("crypto_venue_health", [])
    packet = {
        "purpose": "Read-only state packet for LLM agents. Recommend actions through llm_recommendations_inbox.jsonl only.",
        "mode": settings.get("mode"),
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "summary": payload.get("summary", {}),
        "execution_summary": payload.get("execution_summary", {}),
        "route_resolver": _compact_route_resolver(payload.get("route_resolver", {})),
        "expansion_map": payload.get("expansion_map", {}),
        "global_market_discovery": global_market_discovery,
        "frontier_execution_quality": _compact_frontier_execution_quality(payload.get("research_worker")),
        "hunter_allocation": hunter_allocation,
        "llm_cost_summary": payload.get("llm_cost_summary", {}),
        "llm_inbox": payload.get("llm_inbox", {}),
        "maintenance": payload.get("maintenance", {}),
        "horizon_outcomes": payload.get("horizon_outcomes", []),
        "crypto_venue_health": crypto_venue_health,
        "crypto_venue_health_gaps": _crypto_venue_health_gaps(crypto_venue_health),
        "frontier_crypto_venues": _compact_frontier_crypto(payload.get("frontier_crypto_venues", {})),
        "signal_redesign": _compact_signal_redesign(payload.get("signal_redesign", {})),
        "okx_signal_research": _compact_okx_signal_research(payload.get("okx_signal_research", {})),
        "strategy_reliability": _compact_strategy_reliability(payload.get("strategy_reliability", {})),
        "strategy_lab": payload.get("strategy_lab") or strategy_lab_summary(conn),
        "autonomous_builder": payload.get("autonomous_builder", {}),
        "self_improvement_open_pack": _compact_self_improvement_open_pack(
            payload.get("self_improvement_open_pack")
            or (payload.get("self_improvement", {}) or {}).get("self_improvement_open_pack", {})
        ),
        "contextual_failure_filters": _compact_contextual_failures(payload.get("contextual_failure_filters", {})),
        "buckets": buckets,
        "top_reviewed": payload.get("top_reviewed", [])[:20],
        "recent_opened": payload.get("opened", []),
        "recent_closed": payload.get("closed", []),
        "signal_stats": stats[:50],
        "contextual_stats": contextual_stats,
        "hunter_directives": directives[:50],
        "growth_experiments": experiments[:50],
        "improvement_tasks": tasks[:50],
        "self_improvement": {
            "latest_executor_report": _compact_executor_report(payload.get("self_improvement", {})),
            "active_signal_policies": policies[:50],
            "experiments": self_improvement_experiments[:50],
            "route_probe_tasks": route_probe_tasks,
            "adapter_specs": adapter_specs,
        },
        "code_evolution": code_evolution_summary(conn),
        "memory_artifacts": {
            "latest_markdown": str(RUNS_DIR / "memory_facts_latest.md"),
            "graphiti_export": str(RUNS_DIR / "graphiti_memory_export.jsonl"),
        },
        "allowed_recommendation_actions": allowed_actions,
        "recommendation_schema": _recommendation_schema(allowed_actions),
        "hard_limits": [
            "Do not place live trades.",
            "Do not rewrite code directly except through propose_code_change and the deterministic Build Governor.",
            "Do not run raw installer commands; Python dependencies may be declared in requirements-autonomous.txt or requirements-llm.txt for sandbox-validated installation.",
            "Do not use non-public or illegal data.",
            "Market-expansion code proposals should default to implementation_mode='runtime_active' when they change public-data coverage, scanner breadth, quality scoring, or report/LLM packet wiring.",
            "Use implementation_mode='shadow_trial' only for uncertain new signal logic, not for basic data-coverage expansion.",
            "Autonomous execution is limited to paper-only bounded policies, route probes, adapter specs, memory, reports, tests, and Build-Governor-approved code evolution.",
            "Live trading, credentials, real notional, destructive data changes, startup changes, and broker writes remain blocked.",
            "Global market discovery may research any public market surface worldwide; non-public, stolen, hacked, or credential-only information must not be used as a trading signal.",
            "Recommendation responses must be a single top-level JSON object only; do not emit markdown, commentary, or wrapper arrays.",
        ],
    }
    STATE_JSON.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    STATE_MD.write_text(_packet_to_markdown(packet), encoding="utf-8")
    return packet


def _compact_policy(item: dict) -> dict:
    return {
        "policy_id": item.get("policy_id"),
        "signal_key": item.get("signal_key"),
        "market_key": item.get("market_key"),
        "policy_type": item.get("policy_type"),
        "status": item.get("status"),
        "min_score_delta": item.get("min_score_delta"),
        "min_net_edge_bps": item.get("min_net_edge_bps"),
        "max_spread_bps": item.get("max_spread_bps"),
        "allocation_multiplier": item.get("allocation_multiplier"),
        "pause_entries": item.get("pause_entries"),
        "expires_after_trades": item.get("expires_after_trades"),
        "applied_count": item.get("applied_count"),
        "filtered_count": item.get("filtered_count"),
        "opened_count": item.get("opened_count"),
    }


def _compact_route_resolver(report: dict) -> dict:
    if not report:
        return {}
    summary = report.get("summary", {})
    return {
        "generated_at": report.get("generated_at"),
        "by_route_status": summary.get("by_route_status", {}),
        "by_route_id": summary.get("by_route_id", {}),
        "by_missing_requirement": summary.get("by_missing_requirement", {}),
        "by_requirement_category": summary.get("by_requirement_category", {}),
        "by_requirement_id": summary.get("by_requirement_id", {}),
        "by_route_alternative_status": summary.get("by_route_alternative_status", {}),
        "paper_proxy_available_count": summary.get("paper_proxy_available_count", 0),
        "paper_research_available_count": summary.get("paper_research_available_count", 0),
        "top_manual_actions": summary.get("top_manual_actions", [])[:10],
        "route_intelligence": {
            "blocker_counts": (report.get("route_intelligence") or {}).get("blocker_counts", {}),
            "spot_borrow_assets": (report.get("route_intelligence") or {}).get("spot_borrow_assets", {}),
            "paper_proxy_available_count": (report.get("route_intelligence") or {}).get("paper_proxy_available_count", 0),
            "paper_research_available_count": (report.get("route_intelligence") or {}).get("paper_research_available_count", 0),
            "paper_proxy_available": (report.get("route_intelligence") or {}).get("paper_proxy_available", [])[:10],
            "paper_research_available": (report.get("route_intelligence") or {}).get("paper_research_available", [])[:10],
            "interesting_but_not_executable_count": (report.get("route_intelligence") or {}).get(
                "interesting_but_not_executable_count", 0
            ),
            "potentially_executable_soon_count": (report.get("route_intelligence") or {}).get(
                "potentially_executable_soon_count", 0
            ),
            "route_decision_pack": (report.get("route_intelligence") or {}).get("route_decision_pack", {}),
            "report": str(RUNS_DIR / "route_intelligence_report.md"),
        },
        "hard_limits": report.get("hard_limits", []),
        "report": str(RUNS_DIR / "route_resolver_report.md"),
    }


def _compact_frontier_crypto(report: dict) -> dict:
    if not report:
        return {}
    summary = report.get("summary", {})
    observations = []
    for row in report.get("observations", [])[:20]:
        observations.append(
            {
                "venue": row.get("venue"),
                "region": row.get("region"),
                "market_type": row.get("market_type"),
                "symbol": row.get("symbol"),
                "base": row.get("base"),
                "quote": row.get("quote"),
                "quote_normalization_status": row.get("quote_normalization_status"),
                "quote_normalization_source": row.get("quote_normalization_source"),
                "data_status": row.get("data_status"),
                "http_status": row.get("http_status"),
                "latency_ms": row.get("latency_ms"),
                "last": row.get("last"),
                "spread_bps": row.get("spread_bps"),
                "quote_volume_24h": row.get("quote_volume_24h"),
                "funding_rate": row.get("funding_rate"),
                "quality_status": row.get("quality_status"),
                "quality_score": row.get("quality_score"),
                "freshness_age_seconds": row.get("freshness_age_seconds"),
                "depth_latency_ms": row.get("depth_latency_ms"),
                "anomaly_flags": row.get("anomaly_flags", [])[:5],
                "notes": row.get("notes", [])[:3],
            }
        )
    candidates = []
    for row in report.get("candidates", [])[:20]:
        candidates.append(
            {
                "inst_id": row.get("inst_id"),
                "venue": row.get("venue"),
                "region": row.get("region"),
                "base": row.get("base"),
                "quote": row.get("quote"),
                "quote_normalization_status": row.get("quote_normalization_status"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "edge_bps_estimate": row.get("edge_bps_estimate"),
                "gross_edge_bps_estimate": row.get("gross_edge_bps_estimate"),
                "estimated_round_trip_cost_bps": row.get("estimated_round_trip_cost_bps"),
                "venue_deviation_bps": row.get("venue_deviation_bps"),
                "quality_status": row.get("quality_status"),
                "quality_score": row.get("quality_score"),
                "quality_action": row.get("quality_action"),
                "anomaly_flags": row.get("anomaly_flags", [])[:5],
                "data_status": row.get("data_status"),
                "route_status": (row.get("execution_feasibility") or {}).get("status"),
                "route_blockers": (row.get("execution_feasibility") or {}).get("route_blockers", []),
                "best_route_alternative": (row.get("execution_feasibility") or {}).get("best_route_alternative"),
                "candidate_reject_reason": row.get("candidate_reject_reason"),
            }
        )
    return {
        "generated_at": report.get("generated_at"),
        "summary": summary,
        "observations": observations,
        "candidates": candidates,
        "report": str(RUNS_DIR / "frontier_crypto_venues_report.md"),
    }


def _compact_contextual_failures(report: dict) -> dict:
    if not report:
        return {}
    def compact_context(item: dict) -> dict:
        return {
            "signal_key": item.get("signal_key"),
            "dimension": item.get("dimension"),
            "value": item.get("value"),
            "status": item.get("status"),
            "failure_domain": item.get("failure_domain"),
            "closed_count": item.get("closed_count"),
            "avg_pnl_bps": item.get("avg_pnl_bps"),
            "win_rate": item.get("win_rate"),
            "recent_avg_pnl_bps": item.get("recent_avg_pnl_bps"),
            "recent_win_rate": item.get("recent_win_rate"),
            "worst_bps": item.get("worst_bps"),
            "failure_score": item.get("failure_score"),
            "recovery_score": item.get("recovery_score"),
            "context_filter": item.get("context_filter"),
        }

    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "created_policies": [
            {
                "policy_id": item.get("policy_id"),
                "experiment_id": item.get("experiment_id"),
                "group": compact_context(item.get("group", {})),
                "policy": item.get("policy", {}),
            }
            for item in report.get("created_policies", [])[:10]
        ],
        "top_failing_contexts": [compact_context(item) for item in report.get("top_failing_contexts", [])[:15]],
        "top_working_contexts": [compact_context(item) for item in report.get("top_working_contexts", [])[:10]],
        "recovery_candidates": [compact_context(item) for item in report.get("recovery_candidates", [])[:10]],
        "protected_working_slices": [
            compact_context(item) for item in report.get("protected_working_slices", [])[:10]
        ],
        "route_or_data_quality_failures": [
            compact_context(item) for item in report.get("route_or_data_quality_failures", [])[:10]
        ],
        "report": str(RUNS_DIR / "contextual_failure_report.md"),
    }


def _compact_signal_redesign(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "variants": [
            {
                "variant_id": item.get("variant_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "consecutive_passes": item.get("consecutive_passes"),
                "config": item.get("config"),
                "evaluation": item.get("evaluation"),
            }
            for item in report.get("variants", [])[:15]
        ],
        "evaluations": report.get("evaluations", [])[:10],
        "promotion_blockers": report.get("promotion_blockers", {}),
        "top_failures": report.get("diagnostics", {}).get("top_failures", [])[:10],
        "top_working": report.get("diagnostics", {}).get("top_working", [])[:10],
        "direction_side": report.get("diagnostics", {}).get("direction_side", [])[:10],
        "venue_route_quality_failures": report.get("diagnostics", {}).get("venue_route_quality_failures", [])[:10],
        "promotion_gates": report.get("promotion_gates", {}),
        "report": str(RUNS_DIR / "signal_redesign_report.md"),
    }


def _compact_okx_signal_research(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "variants": [
            {
                "variant_id": item.get("variant_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "consecutive_passes": item.get("consecutive_passes"),
                "config": item.get("config"),
                "evaluation": item.get("evaluation"),
            }
            for item in report.get("variants", [])[:15]
        ],
        "evaluations": report.get("evaluations", [])[:10],
        "top_failures": report.get("diagnostics", {}).get("top_failures", [])[:10],
        "top_working": report.get("diagnostics", {}).get("top_working", [])[:10],
        "carry_economics": report.get("carry_economics", {}),
        "promotion_gates": report.get("promotion_gates", {}),
        "report": str(RUNS_DIR / "okx_signal_research_report.md"),
    }


def _compact_strategy_reliability(report: dict) -> dict:
    if not report:
        return {}
    return {
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary", {}),
        "top_adjustments": report.get("top_adjustments", [])[:15],
        "covered_improvement_task_ids": report.get("covered_improvement_task_ids", []),
        "covered_growth_experiment_ids": report.get("covered_growth_experiment_ids", []),
        "report": str(RUNS_DIR / "strategy_reliability_report.md"),
    }


def _compact_self_improvement_open_pack(report: dict) -> dict:
    if not report:
        return {}
    borrow = report.get("route_borrow_intelligence") or {}
    africa = report.get("africa_rail_watchlist") or {}
    kalshi = report.get("kalshi_public_coverage") or {}
    diagnostics = report.get("signal_repair_diagnostics") or {}
    return {
        "generated_at": report.get("generated_at"),
        "paper_only": report.get("paper_only"),
        "covered_improvement_task_ids": report.get("covered_improvement_task_ids", [])[:30],
        "covered_growth_experiment_ids": report.get("covered_growth_experiment_ids", [])[:30],
        "route_borrow": {
            "record_count": borrow.get("record_count", 0),
            "shadow_only_unconfirmed_count": borrow.get("shadow_only_unconfirmed_count", 0),
            "by_venue": borrow.get("by_venue", {}),
            "top_records": (borrow.get("records") or [])[:10],
        },
        "africa_rails": {
            "venue_count": africa.get("venue_count", 0),
            "instrument_count": africa.get("instrument_count", 0),
            "by_venue_availability": africa.get("by_venue_availability", {}),
            "rails": (africa.get("rails") or [])[:6],
        },
        "kalshi_public_coverage": {
            "current_candidate_count": kalshi.get("current_candidate_count", 0),
            "route_status": kalshi.get("route_status"),
            "route_blockers": kalshi.get("route_blockers", {}),
            "orderbook_status_counts": kalshi.get("orderbook_status_counts", {}),
        },
        "signal_repair_diagnostics": {
            "active_loosenings_created": diagnostics.get("active_loosenings_created", 0),
            "frontier_count": len(diagnostics.get("frontier_weak_signal_diagnostics", [])),
            "yahoo_count": len(diagnostics.get("yahoo_proxy_diagnostics", [])),
            "okx_count": len(diagnostics.get("okx_basis_funding_diagnostics", [])),
            "positive_shadow_expansions": len(diagnostics.get("positive_shadow_expansion_variants", [])),
            "top_frontier": (diagnostics.get("frontier_weak_signal_diagnostics") or [])[:10],
        },
        "report": str(RUNS_DIR / "self_improvement_open_pack.md"),
    }


def _compact_executor_report(report: dict) -> dict:
    if not report:
        return {}
    return {
        "enabled": report.get("enabled"),
        "generated_at": report.get("generated_at"),
        "consumed_count": len(report.get("consumed", [])),
        "evaluated_count": len(report.get("evaluated", [])),
        "superseded_count": len(report.get("superseded", [])),
        "active_policy_count": len(report.get("active_policies", [])),
        "route_probe_task_count": len(report.get("route_probe_tasks", [])),
        "adapter_spec_count": len(report.get("adapter_specs", [])),
        "consumed": [
            {
                "task_type": item.get("task_type"),
                "title": item.get("title"),
                "created_count": len(
                    [row for row in item.get("created", []) if row.get("action_status", "created") == "created"]
                ),
                "skipped_count": len([row for row in item.get("created", []) if row.get("action_status") == "skipped"]),
            }
            for item in report.get("consumed", [])[:10]
        ],
        "evaluated": report.get("evaluated", [])[:10],
    }


def _compact_experiment(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "source_agent": item.get("source_agent"),
        "task_type": item.get("task_type"),
        "priority": item.get("priority"),
        "market_key": item.get("market_key"),
        "signal_key": item.get("signal_key"),
        "hypothesis": item.get("hypothesis"),
        "status": item.get("status"),
        "decision": item.get("decision"),
        "baseline": item.get("baseline"),
        "evaluation": item.get("evaluation"),
    }


def _compact_global_market_discovery(report: dict | None = None) -> dict:
    if not report:
        path = RUNS_DIR / "research_worker_latest.json"
        if path.exists():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                report = {"status": "unreadable", "report": str(path)}
    if not report:
        return {
            "status": "missing",
            "report": str(RUNS_DIR / "research_worker_report.md"),
            "candidate_ledger": str(RUNS_DIR / "market_discovery_candidates.jsonl"),
        }
    summary = report.get("summary", {})
    return {
        "status": report.get("status"),
        "global_market_discovery": report.get("global_market_discovery"),
        "web_research_enabled": report.get("web_research_enabled"),
        "candidate_count": summary.get("candidate_count", 0),
        "new_candidate_count": summary.get("new_candidate_count", 0),
        "total_known_candidate_count": summary.get("total_known_candidate_count", 0),
        "by_surface_type": summary.get("by_surface_type", {}),
        "by_region": summary.get("by_region", {}),
        "by_recommended_next_action": summary.get("by_recommended_next_action", {}),
        "inserted_artifact_counts": summary.get("inserted_artifact_counts", {}),
        "top_candidates": summary.get("top_candidates", [])[:10],
        "report": str(RUNS_DIR / "research_worker_report.md"),
        "candidate_ledger": str(RUNS_DIR / "market_discovery_candidates.jsonl"),
    }


def _packet_to_markdown(packet: dict) -> str:
    lines = [
        "# LLM State Packet",
        "",
        packet["purpose"],
        "",
        f"- Mode: `{packet['mode']}`",
        f"- Live trading allowed: `{packet['live_trading_allowed']}`",
        f"- Summary: `{packet['summary']}`",
        f"- Execution: `{packet['execution_summary']}`",
        f"- Route resolver: `{packet.get('route_resolver', {})}`",
        f"- Expansion map: `{packet.get('expansion_map', {})}`",
        f"- Global market discovery: `{packet.get('global_market_discovery', {})}`",
        f"- Hunter allocation: `{packet.get('hunter_allocation', {})}`",
        f"- Frontier crypto venues: `{packet.get('frontier_crypto_venues', {})}`",
        f"- Signal redesign: `{packet.get('signal_redesign', {})}`",
        f"- OKX signal research: `{packet.get('okx_signal_research', {})}`",
        f"- Strategy reliability: `{packet.get('strategy_reliability', {})}`",
        f"- Strategy Lab: `{packet.get('strategy_lab', {})}`",
        f"- Self-improvement open pack: `{packet.get('self_improvement_open_pack', {})}`",
        f"- Code evolution: `{packet.get('code_evolution', {})}`",
        f"- Contextual failure filters: `{packet.get('contextual_failure_filters', {})}`",
        f"- LLM cost: `{packet['llm_cost_summary']}`",
        "",
        "## Exploit",
        "",
    ]
    for item in packet["buckets"]["exploit"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Explore", ""])
    for item in packet["buckets"]["explore"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Diagnose", ""])
    for item in packet["buckets"]["diagnose"][:20]:
        lines.append(f"- `{item.get('signal_key') or item.get('market_key')}`: {item.get('reason') or item.get('rationale')}")
    lines.extend(["", "## Self-Improvement", ""])
    for policy in packet.get("self_improvement", {}).get("active_signal_policies", [])[:10]:
        lines.append(
            f"- Active policy `{policy['policy_id']}` for `{policy['signal_key']}` "
            f"filtered={policy.get('filtered_count')} opened={policy.get('opened_count')}"
        )
    for exp in packet.get("self_improvement", {}).get("experiments", [])[:10]:
        lines.append(f"- Experiment #{exp['id']} `{exp['task_type']}` status={exp['status']} signal=`{exp.get('signal_key')}`")
    lines.extend(["", "## How To Recommend", ""])
    lines.append(f"Write JSONL recommendations to `{INBOX}` using actions: {packet['allowed_recommendation_actions']}")
    lines.append("")
    lines.append("Example:")
    lines.append("")
    lines.append("```json")
    lines.append('{"action":"request_market_adapter","priority":85,"title":"Add prediction market scanner","rationale":"Prediction markets may expose event-latency edges.","market_key":"prediction_markets","evidence":{"reason":"uncovered market surface"},"proposed_change":"Build public Kalshi/Polymarket scanner."}')
    lines.append("```")
    return "\n".join(lines) + "\n"


def ingest_llm_recommendations(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("llm_bridge", {})
    if not cfg.get("enabled", True) or not cfg.get("ingest_recommendations", True):
        return []
    if not INBOX.exists():
        return []

    allowed = set(cfg.get("allowed_actions", []))
    max_items = int(cfg.get("max_recommendations_per_loop", 20))
    accepted = []
    remaining = []
    lines = INBOX.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if len(accepted) >= max_items:
            remaining.append(line)
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = item.get("action")
        if action not in allowed:
            continue
        title = str(item.get("title") or action)[:180]
        rationale = str(item.get("rationale") or item.get("proposed_change") or "")[:2000]
        try:
            priority = int(float(item.get("priority", 50)))
        except (TypeError, ValueError):
            priority = {
                "critical": 100,
                "urgent": 95,
                "highest": 95,
                "high": 90,
                "medium_high": 80,
                "medium-high": 80,
                "medium": 60,
                "normal": 50,
                "low": 35,
            }.get(str(item.get("priority") or "").strip().lower(), 50)
        priority = max(1, min(100, priority))
        rec_id = hashlib.sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()
        if not add_llm_recommendation(conn, rec_id, action, title, rationale, item):
            continue
        _apply_recommendation(conn, action, title, rationale, priority, item)
        accepted.append({"id": rec_id, "action": action, "title": title, "priority": priority})
        with PROCESSED.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": rec_id, "item": item}, sort_keys=True) + "\n")

    INBOX.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
    return accepted


def _implemented_manual_category(text: str) -> str | None:
    text = text.lower()
    if is_duplicate_open_pack_text(text):
        return "self_improvement_open_pack"
    global_discovery_implemented = (
        "global_discovery|" in text
        or any(term in text for term in GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS)
        or (
            any(term in text for term in ("global market discovery", "global_market_discovery", "global discovery"))
            and any(term in text for term in ("scanner", "scan", "proxy", "seed", "surface list", "coverage map"))
        )
    )
    if global_discovery_implemented and not any(
        term in text for term in ("new unlisted market", "new venue not in scanner", "add unseen")
    ):
        return "global_market_discovery_scan"
    if any(
        term in text
        for term in (
            "bybit quality",
            "bybit long-frontier",
            "bybit long frontier",
            "expand profitable bybit",
            "tighten bybit",
            "bybit decay",
        )
    ):
        return "bybit_quality_decay_expansion_pack"
    if any(
        term in text
        for term in (
            "kucoin long-frontier",
            "kucoin long frontier",
            "tighten kucoin",
            "kucoin weak",
            "kucoin repair",
        )
    ):
        return "kucoin_long_repair_diagnostics"
    if any(
        term in text
        for term in (
            "strategy reliability",
            "venue-direction reliability",
            "venue direction reliability",
            "frontier venue signal repair",
            "frontier long weak",
            "frontier short weak",
            "long-frontier",
            "short-frontier",
            "yahoo proxy short",
            "proxy short",
            "funding/basis split",
            "funding basis split",
            "positive slice expansion",
            "market-specific factors",
            "microstructure and liquidity",
            "microstructure divergence",
            "weak win-rate",
            "weak win rate",
            "expand okx funding",
            "expand gate frontier short",
            "expand mexc frontier short",
            "expand binance_us frontier short",
        )
    ):
        return "strategy_reliability_pack"
    if "okx" in text and any(
        term in text
        for term in (
            "reliable outcome",
            "reliable label",
            "legacy_unverified",
            "variant learning",
            "valid labels",
        )
    ):
        return "okx_reliable_outcomes"
    if "frontier" in text and any(
        term in text
        for term in (
            "systemic",
            "negative performance",
            "poor signal performance",
            "venue-map",
            "venue map",
            "underserved frontier",
        )
    ):
        return "frontier_systemic_redesign"
    if "okx" in text and "perp" in text and "funding" in text and any(
        term in text
        for term in (
            "basis signal",
            "basis signals",
            "basis signal research",
            "investigate and improve okx",
            "funding basis signal",
            "funding basis signals",
        )
    ):
        return "okx_basis_signal_research"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "africa",
            "southeast asia",
            "emerging frontier",
            "regional frontier",
            "regional venue",
        )
    ):
        return "regional_frontier_data"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "enhanced data coverage",
            "data quality",
            "order book",
            "orderbook",
            "market depth",
            "freshness",
            "slippage",
            "liquidity quality",
        )
    ):
        return "frontier_data_quality"
    if any(
        term in text
        for term in (
            "regional fx",
            "fx reference",
            "public fx midpoint",
            "fiat-stablecoin reference",
            "quote normalization",
            "africa rail",
            "african stablecoin",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if "frontier" in text and any(
        term in text
        for term in (
            "adaptive depth",
            "depth enrichment",
            "known quality",
            "quality coverage",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if "prediction" in text and any(
        term in text
        for term in (
            "event classification",
            "event intelligence",
            "expired",
            "resolution",
            "order-book",
            "orderbook",
        )
    ):
        return "regional_fx_frontier_prediction_pack"
    if any(
        term in text
        for term in (
            "signal redesign",
            "root cause analysis",
            "root-cause analysis",
            "investigate and improve poorly performing signals",
            "improve frontier crypto spot signals across venues",
        )
    ):
        return "signal_redesign"
    if (
        "route requirement" in text
        or "execution route requirements" in text
        or ("conditional opportunit" in text and "requirements" in text)
    ):
        return "route_requirements"
    if "frontier crypto" in text and any(
        term in text
        for term in (
            "data adapter",
            "market coverage",
            "venue adapter",
            "undercovered",
            "poor performance",
        )
    ):
        return "frontier_crypto_adapter"
    if any(
        term in text
        for term in (
            "failure filter",
            "demote or filter",
            "demote and filter",
            "tighten filters for losing signal",
        )
    ):
        return "failure_diagnostics"
    return None


def _implemented_manual_category_exists(conn: sqlite3.Connection, category: str | None) -> bool:
    if not category or category not in IMPLEMENTED_MANUAL_STATUSES:
        return False
    status, tables = IMPLEMENTED_MANUAL_STATUSES[category]
    for table in tables:
        row = conn.execute(f"select 1 from {table} where status = ? limit 1", (status,)).fetchone()
        if row:
            return True
    return False


def _apply_recommendation(
    conn: sqlite3.Connection,
    action: str,
    title: str,
    rationale: str,
    priority: int,
    item: dict,
) -> None:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    signal_key = item.get("signal_key") or item.get("market_key") or "llm_recommendation"
    proposed_change = str(item.get("proposed_change") or rationale)

    if action in {"propose_build_task", "request_data_source", "request_market_adapter", "request_red_team"}:
        duplicate_category = _implemented_manual_category(" ".join([title, rationale, proposed_change]))
        if _implemented_manual_category_exists(conn, duplicate_category):
            return
        add_improvement_task(conn, priority, f"LLM: {title}", proposed_change or rationale)
    elif action == "propose_growth_experiment":
        duplicate_category = _implemented_manual_category(" ".join([title, rationale, proposed_change, signal_key]))
        if _implemented_manual_category_exists(conn, duplicate_category):
            return
        add_growth_experiment(conn, priority, signal_key, title, proposed_change, evidence)
    elif action == "propose_hunter_directive":
        directive = str(item.get("directive") or "llm_research_directive")
        add_hunter_directive(conn, signal_key, directive, priority, rationale, evidence)
