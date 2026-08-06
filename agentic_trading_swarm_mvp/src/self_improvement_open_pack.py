"""Consolidated paper-only implementation pack for the current open queue.

This module turns the current self-improvement backlog into concrete read-only
and paper-only system behavior.  It does not fetch credentials, call private
APIs, place orders, or enable live trading.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import sqlite3
import statistics
from collections.abc import Iterable, Mapping
from typing import Any

from storage import RUNS_DIR


IMPLEMENTED_STATUS = "implemented_self_improvement_open_pack_2026_06_29"
DEDUPED_STATUS = "deduplicated_into_self_improvement_open_pack_2026_06_29"

REPORT_JSON = RUNS_DIR / "self_improvement_open_pack.json"
REPORT_MD = RUNS_DIR / "self_improvement_open_pack.md"

COVERED_IMPROVEMENT_TASK_IDS = (
    80027,
    81865,
    82565,
    97259,
    100944,
    106319,
    116738,
    103788,
    103733,
    87611,
    114417,
    80028,
    81866,
    100945,
    106320,
    84102,
    84309,
)

COVERED_GROWTH_EXPERIMENT_IDS = (
    78527,
    66754,
    92249,
    92200,
    66755,
    71274,
    73058,
    73725,
    86398,
    89743,
    94508,
    79115,
    74464,
    75346,
    75956,
    88660,
)

DEDUPED_GROWTH_EXPERIMENT_IDS = (
    74464,
    75346,
    75956,
    88660,
)

OPEN_PACK_DUPLICATE_TERMS = (
    "spot-borrow route",
    "spot borrow route",
    "borrow route data",
    "borrow blockers",
    "africa fiat crypto rail",
    "africa stablecoin rails",
    "yellow card",
    "bitnob",
    "kalshi read-only",
    "kalshi public",
    "frontier weak",
    "tighten filters bitget",
    "tighten filters okx_spot",
    "tighten filters kraken",
    "tighten filters luno",
    "red-team bitget",
    "red-team okx_spot",
    "red-team luno",
    "yahoo proxy regime",
    "okx basis conditional",
    "quarantine okx basis",
    "positive okx_spot frontier",
)

BORROW_DOC_SOURCES = {
    "BINANCE_US": "https://docs.binance.us/",
    "BITGET": "https://www.bitget.com/api-doc/spot/market/Get-Orderbook",
    "COINBASE": "https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/introduction",
    "GATE": "https://www.gate.com/docs/developers/apiv4/en/",
    "KRAKEN": "https://docs.kraken.com/api/",
    "KUCOIN": "https://www.kucoin.com/docs-new/rest/spot-trading/market-data/get-part-orderbook",
    "LUNO": "https://www.luno.com/en/developers/api",
    "MEXC": "https://mexcdevelop.github.io/apidocs/spot_v3_en/",
    "OKX": "https://www.okx.com/docs-v5/en/",
    "OKX_SPOT": "https://www.okx.com/docs-v5/en/",
}

AFRICA_RAILS = (
    {
        "venue": "VALR",
        "region": "africa",
        "fiat_currencies": ["ZAR"],
        "watch_instruments": ["BTC/ZAR", "ETH/ZAR", "USDT/ZAR", "USDC/ZAR"],
        "venue_availability": "public_market_data_available",
        "quote_status": "public_book_or_ticker_through_frontier_depth_layer",
        "fee_disclosure": "public_docs_or_exchange_fee_page_required",
        "source_url": "https://docs.valr.com/",
    },
    {
        "venue": "LUNO",
        "region": "africa",
        "fiat_currencies": ["NGN", "ZAR", "KES", "GHS"],
        "watch_instruments": ["BTC/NGN", "BTC/ZAR", "ETH/ZAR", "USDC/ZAR"],
        "venue_availability": "public_market_data_available_where_pair_listed",
        "quote_status": "public_book_or_ticker_through_frontier_depth_layer",
        "fee_disclosure": "public_docs_or_exchange_fee_page_required",
        "source_url": "https://www.luno.com/en/developers/api",
    },
    {
        "venue": "YELLOW_CARD",
        "region": "africa",
        "fiat_currencies": ["NGN", "KES", "GHS", "ZAR"],
        "watch_instruments": ["USDT/NGN", "USDT/KES", "USDC/GHS", "BTC/NGN"],
        "venue_availability": "watch_only_no_public_order_book_configured",
        "quote_status": "indicative_or_private_only_until_public_quote_confirmed",
        "fee_disclosure": "unknown_until_public_source_verified",
        "source_url": "https://docs.yellowcard.engineering/docs/getting-started",
    },
    {
        "venue": "BITNOB",
        "region": "africa",
        "fiat_currencies": ["NGN", "KES", "GHS"],
        "watch_instruments": ["USDT/NGN", "BTC/NGN", "USDC/KES", "BTC/GHS"],
        "venue_availability": "watch_only_no_public_order_book_configured",
        "quote_status": "indicative_or_private_only_until_public_quote_confirmed",
        "fee_disclosure": "unknown_until_public_source_verified",
        "source_url": "https://bitnob.dev/",
    },
)

WEAK_SIGNAL_TARGETS = (
    ("BITGET", "short", "frontier"),
    ("OKX_SPOT", "short", "frontier"),
    ("KRAKEN", "short", "frontier"),
    ("LUNO", "short", "frontier"),
    ("LUNO", "long", "frontier"),
    ("YAHOO_PROXY", "long_proxy", "global_proxy"),
    ("YAHOO_PROXY", "short_proxy", "global_proxy"),
    ("OKX", "basis", "perp_funding_basis"),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def is_duplicate_open_pack_text(text: str) -> bool:
    """Return true when a future LLM/manual item is covered by this pack."""

    lowered = text.lower()
    return any(term in lowered for term in OPEN_PACK_DUPLICATE_TERMS)


def build_spot_borrow_intelligence(candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = []
    seen = set()
    for candidate in candidates:
        route = candidate.get("execution_route") or {}
        blockers = _route_blockers(candidate, route)
        direction = str(candidate.get("direction") or route.get("direction") or "")
        if "spot_borrow" not in blockers and "short_frontier_spot" not in direction:
            continue
        venue = _venue(candidate)
        inst_id = str(candidate.get("inst_id") or route.get("inst_id") or "unknown")
        key = (venue, inst_id, direction)
        if key in seen:
            continue
        seen.add(key)
        record = _borrow_record(candidate, route, venue, inst_id, direction, blockers)
        records.append(record)

    records.sort(key=lambda row: (0 if row["shadow_only_until_confirmed"] else 1, row["venue"], row["instrument"]))
    by_venue = collections.Counter(row["venue"] for row in records)
    by_status = collections.Counter(row["borrow_availability"] for row in records)
    return {
        "paper_only": True,
        "record_count": len(records),
        "by_venue": dict(by_venue),
        "by_borrow_availability": dict(by_status),
        "shadow_only_unconfirmed_count": sum(1 for row in records if row["shadow_only_until_confirmed"]),
        "records": records[:50],
        "hard_limits": [
            "Read-only borrow intelligence.",
            "No credentials, private margin APIs, account enablement, or order routes are used.",
            "Unconfirmed short-spot ideas remain shadow-only unless borrow support is confirmed and net edge remains positive.",
        ],
    }


def build_africa_rail_watchlist(limit_venues: int = 4, limit_instruments: int = 16) -> dict[str, Any]:
    rows = []
    instrument_count = 0
    for rail in AFRICA_RAILS[:limit_venues]:
        instruments = list(rail["watch_instruments"])[: max(0, limit_instruments - instrument_count)]
        instrument_count += len(instruments)
        rows.append(
            {
                **rail,
                "watch_instruments": instruments,
                "instrument_count": len(instruments),
                "public_indicative_bid": None,
                "public_indicative_ask": None,
                "quote_age_seconds": None,
                "credential_required": False,
                "route_status": "watch_only_public_or_indicative_data",
            }
        )
        if instrument_count >= limit_instruments:
            break
    by_status = collections.Counter(row["venue_availability"] for row in rows)
    return {
        "paper_only": True,
        "venue_cap": limit_venues,
        "instrument_cap": limit_instruments,
        "venue_count": len(rows),
        "instrument_count": instrument_count,
        "by_venue_availability": dict(by_status),
        "rails": rows,
        "hard_limits": [
            "Africa rail coverage is watch-only unless public data, freshness, quote normalization, and quality gates pass.",
            "No private quote endpoints, user accounts, credentials, or transfer rails are called.",
        ],
    }


def build_kalshi_public_coverage(prediction_summary: Mapping[str, Any] | None) -> dict[str, Any]:
    summary = prediction_summary or {}
    by_venue = summary.get("by_venue", {}) if isinstance(summary.get("by_venue"), Mapping) else {}
    orderbooks = summary.get("by_orderbook_status", {}) if isinstance(summary.get("by_orderbook_status"), Mapping) else {}
    blockers = summary.get("route_blockers", {}) if isinstance(summary.get("route_blockers"), Mapping) else {}
    kalshi_count = int(by_venue.get("KALSHI", 0) or 0)
    kalshi_blockers = {key: kalshi_count for key in ("prediction_markets_account", "venue_api_access", "jurisdiction_eligibility")}
    return {
        "paper_only": True,
        "enabled": True,
        "venue": "KALSHI",
        "market_cap_per_run": 50,
        "current_candidate_count": kalshi_count,
        "orderbook_status_counts": dict(orderbooks),
        "route_status": "conditional",
        "route_blockers": kalshi_blockers,
        "all_prediction_route_blockers": dict(blockers),
        "normalized_fields": [
            "ticker",
            "title",
            "category",
            "close_time",
            "yes_bid",
            "yes_ask",
            "no_bid",
            "no_ask",
            "last_price",
            "volume",
            "liquidity",
            "open_interest",
            "settlement_status",
        ],
        "hard_limits": [
            "Kalshi remains public-data/read-only.",
            "Prediction routes stay conditional until account, API, and jurisdiction requirements are externally confirmed.",
        ],
    }


def _report_metrics(values: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    if not samples:
        return {
            "count": 0,
            "avg_pnl_bps": None,
            "median_pnl_bps": None,
            "win_rate": None,
            "worst_decile_pnl_bps": None,
        }
    ordered = sorted(samples)
    decile_count = max(1, int(len(ordered) * 0.1))
    return {
        "count": len(samples),
        "avg_pnl_bps": round(statistics.fmean(samples), 3),
        "median_pnl_bps": round(statistics.median(samples), 3),
        "win_rate": round(sum(value > 0 for value in samples) / len(samples), 3),
        "worst_decile_pnl_bps": round(statistics.fmean(ordered[:decile_count]), 3),
    }


def _safe_json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _lookup_nested(dicts: Iterable[Mapping[str, Any]], *keys: str) -> object:
    for mapping in dicts:
        for key in keys:
            if key not in mapping:
                continue
            value = mapping.get(key)
            if isinstance(value, str):
                if value.strip():
                    return value.strip()
                continue
            if value is not None:
                return value
    return None


def _as_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal_age_bucket(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "unknown"
    if age_seconds <= 15 * 60:
        return "fresh_le_15m"
    if age_seconds <= 30 * 60:
        return "aging_15_to_30m"
    if age_seconds <= 60 * 60:
        return "stale_30_to_60m"
    return "stale_gt_60m"


def _spread_bucket(spread_bps: float | None) -> str:
    if spread_bps is None:
        return "unknown"
    if spread_bps <= 3.0:
        return "tight_le_3bps"
    if spread_bps <= 8.0:
        return "normal_3_to_8bps"
    if spread_bps <= 15.0:
        return "wide_8_to_15bps"
    return "extreme_gt_15bps"


def _proxy_cohort_label(candidate: Mapping[str, Any], context: Mapping[str, Any], review: Mapping[str, Any]) -> str:
    direct = _lookup_nested(
        (candidate, context, review),
        "proxy_cohort",
        "paper_proxy_cohort",
        "cohort_surface",
        "cohort",
        "region_timezone_cohort",
    )
    if direct not in (None, ""):
        return str(direct)
    region = str(_lookup_nested((candidate, context, review), "region") or "unknown").strip().lower()
    timezone = str(
        _lookup_nested(
            (candidate, context, review),
            "timezone",
            "market_timezone",
            "exchange_timezone",
            "region_timezone",
        )
        or "unknown"
    ).strip()
    return f"{region}|{timezone}"


def _route_surface_label(candidate: Mapping[str, Any], context: Mapping[str, Any], review: Mapping[str, Any]) -> str:
    route = candidate.get("execution_route")
    route_dict = dict(route) if isinstance(route, Mapping) else {}
    return str(
        _lookup_nested(
            (candidate, context, review, route_dict),
            "route_surface",
            "paper_execution_semantics",
            "source_surface",
            "execution_surface",
        )
        or "unknown"
    )


def _signal_age_seconds(candidate: Mapping[str, Any], context: Mapping[str, Any], review: Mapping[str, Any]) -> float | None:
    direct = _as_float(
        _lookup_nested(
            (candidate, context, review),
            "signal_age_seconds",
            "provider_age_seconds",
            "freshness_age_seconds",
            "quote_age_seconds",
            "proxy_age_seconds",
        )
    )
    if direct is not None:
        return max(0.0, direct)
    stale_minutes = _as_float(_lookup_nested((candidate, context, review), "stale_minutes"))
    if stale_minutes is not None:
        return max(0.0, stale_minutes * 60.0)
    return None


def _aggregate_labeled_metrics(
    labels_to_values: Mapping[str, list[float]],
    key_name: str,
    *,
    minimum_count: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, values in labels_to_values.items():
        metrics = _report_metrics(values)
        if metrics["count"] < minimum_count:
            continue
        rows.append({key_name: label, **metrics})
    rows.sort(
        key=lambda item: (
            item.get("avg_pnl_bps") is None,
            item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else 0.0,
            str(item.get(key_name) or ""),
        )
    )
    return rows


def _yahoo_proxy_decay_analysis(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        select p.id, p.direction, p.signal_key, p.context_json, p.candidate_json, p.review_json,
               o.horizon_minutes, o.pnl_bps
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where p.venue = 'YAHOO_PROXY'
          and p.trade_type = 'global_proxy_momentum'
          and (p.strategy_lab_id is null or p.strategy_lab_id = '')
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
        order by p.id asc, o.horizon_minutes asc
        """
    ).fetchall()
    if not rows:
        return {
            "paper_only": True,
            "reliable_label_count": 0,
            "unique_trade_count": 0,
            "primary_horizon_minutes": None,
            "direction_horizon_curves": {},
            "route_surface_outcomes": [],
            "proxy_cohort_outcomes": [],
            "signal_age_outcomes": [],
            "spread_bucket_outcomes": [],
            "regional_timezone_outcomes": [],
            "localization_summary": {
                "localized_decay_detected": False,
                "broad_family_degradation": False,
                "likely_decay_sources": [],
            },
        }

    by_horizon_count: collections.Counter[int] = collections.Counter()
    all_primary_candidates: list[dict[str, Any]] = []
    direction_horizon_values: dict[str, dict[int, list[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for row in rows:
        horizon = int(row["horizon_minutes"])
        by_horizon_count[horizon] += 1
        candidate = _safe_json_dict(row["candidate_json"])
        context = _safe_json_dict(row["context_json"])
        review = _safe_json_dict(row["review_json"])
        direction = str(row["direction"] or "unknown")
        pnl = float(row["pnl_bps"])
        signal_age_seconds = _signal_age_seconds(candidate, context, review)
        spread_bps = _as_float(_lookup_nested((candidate, context, review), "spread_bps", "current_spread_bps"))
        direction_horizon_values[direction][horizon].append(pnl)
        all_primary_candidates.append(
            {
                "trade_id": int(row["id"]),
                "direction": direction,
                "horizon_minutes": horizon,
                "pnl_bps": pnl,
                "route_surface": _route_surface_label(candidate, context, review),
                "proxy_cohort": _proxy_cohort_label(candidate, context, review),
                "signal_age_seconds": signal_age_seconds,
                "signal_age_bucket": _signal_age_bucket(signal_age_seconds),
                "spread_bps": spread_bps,
                "spread_bucket": _spread_bucket(spread_bps),
                "region": str(_lookup_nested((candidate, context, review), "region") or "unknown"),
                "timezone": str(
                    _lookup_nested((candidate, context, review), "timezone", "market_timezone", "exchange_timezone")
                    or "unknown"
                ),
            }
        )

    primary_horizon = 60 if by_horizon_count.get(60) else min(by_horizon_count)
    primary_rows = [row for row in all_primary_candidates if row["horizon_minutes"] == primary_horizon]

    route_values: dict[str, list[float]] = collections.defaultdict(list)
    cohort_values: dict[str, list[float]] = collections.defaultdict(list)
    age_values: dict[str, list[float]] = collections.defaultdict(list)
    spread_values: dict[str, list[float]] = collections.defaultdict(list)
    regional_timezone_values: dict[str, list[float]] = collections.defaultdict(list)
    for row in primary_rows:
        route_values[str(row["route_surface"])].append(float(row["pnl_bps"]))
        cohort_values[str(row["proxy_cohort"])].append(float(row["pnl_bps"]))
        age_values[str(row["signal_age_bucket"])].append(float(row["pnl_bps"]))
        spread_values[str(row["spread_bucket"])].append(float(row["pnl_bps"]))
        regional_timezone_values[f"{row['region']}|{row['timezone']}"].append(float(row["pnl_bps"]))

    direction_curves = {
        direction: {
            str(horizon): _report_metrics(values)
            for horizon, values in sorted(horizons.items())
        }
        for direction, horizons in sorted(direction_horizon_values.items())
    }
    overall_primary = _report_metrics(row["pnl_bps"] for row in primary_rows)
    route_rows = _aggregate_labeled_metrics(route_values, "route_surface")
    cohort_rows = _aggregate_labeled_metrics(cohort_values, "proxy_cohort")
    age_rows = _aggregate_labeled_metrics(age_values, "signal_age_bucket")
    spread_rows = _aggregate_labeled_metrics(spread_values, "spread_bucket")
    regional_timezone_rows = _aggregate_labeled_metrics(regional_timezone_values, "regional_timezone_cohort")

    likely_sources: list[str] = []
    overall_avg = overall_primary.get("avg_pnl_bps")
    stale_avg = next((row["avg_pnl_bps"] for row in age_rows if row["signal_age_bucket"] == "stale_gt_60m"), None)
    fresh_avg = next((row["avg_pnl_bps"] for row in age_rows if row["signal_age_bucket"] == "fresh_le_15m"), None)
    if stale_avg is not None and fresh_avg is not None and stale_avg + 8.0 <= fresh_avg:
        likely_sources.append("stale_proxy_concentration")
    extreme_spread_avg = next((row["avg_pnl_bps"] for row in spread_rows if row["spread_bucket"] == "extreme_gt_15bps"), None)
    tight_spread_avg = next((row["avg_pnl_bps"] for row in spread_rows if row["spread_bucket"] == "tight_le_3bps"), None)
    if extreme_spread_avg is not None and tight_spread_avg is not None and extreme_spread_avg + 8.0 <= tight_spread_avg:
        likely_sources.append("wide_spread_cost_concentration")
    if route_rows and len(route_rows) >= 2:
        best_route = max(route_rows, key=lambda item: item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else float("-inf"))
        worst_route = min(route_rows, key=lambda item: item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else float("inf"))
        if (
            best_route.get("avg_pnl_bps") is not None
            and worst_route.get("avg_pnl_bps") is not None
            and worst_route["avg_pnl_bps"] + 8.0 <= best_route["avg_pnl_bps"]
        ):
            likely_sources.append("route_surface_mismatch")
    if cohort_rows and len(cohort_rows) >= 2:
        best_cohort = max(cohort_rows, key=lambda item: item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else float("-inf"))
        worst_cohort = min(cohort_rows, key=lambda item: item.get("avg_pnl_bps") if item.get("avg_pnl_bps") is not None else float("inf"))
        if (
            best_cohort.get("avg_pnl_bps") is not None
            and worst_cohort.get("avg_pnl_bps") is not None
            and worst_cohort["avg_pnl_bps"] + 8.0 <= best_cohort["avg_pnl_bps"]
        ):
            likely_sources.append("regional_time_zone_cohort_mismatch")
    long_curve = (direction_curves.get("long_proxy") or {}).get(str(primary_horizon), {})
    short_curve = (direction_curves.get("short_proxy") or {}).get(str(primary_horizon), {})
    long_avg = long_curve.get("avg_pnl_bps")
    short_avg = short_curve.get("avg_pnl_bps")
    if long_avg is not None and short_avg is not None and abs(long_avg - short_avg) >= 8.0:
        likely_sources.append("directional_asymmetry")

    localized = bool(likely_sources)
    broad_family_degradation = bool(
        overall_avg is not None and overall_avg < 0.0 and not localized
    )
    return {
        "paper_only": True,
        "reliable_label_count": len(rows),
        "unique_trade_count": len({int(row["id"]) for row in rows}),
        "primary_horizon_minutes": primary_horizon,
        "direction_horizon_curves": direction_curves,
        "route_surface_outcomes": route_rows,
        "proxy_cohort_outcomes": cohort_rows,
        "signal_age_outcomes": age_rows,
        "spread_bucket_outcomes": spread_rows,
        "regional_timezone_outcomes": regional_timezone_rows,
        "localization_summary": {
            "localized_decay_detected": localized,
            "broad_family_degradation": broad_family_degradation,
            "overall_primary_horizon": overall_primary,
            "likely_decay_sources": likely_sources,
        },
    }


def build_signal_repair_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    signal_rows = _load_signal_stats(conn)
    frontier = []
    yahoo = []
    okx = []
    positive_expansion = []
    yahoo_decay_analysis = _yahoo_proxy_decay_analysis(conn)
    for row in signal_rows:
        signal = str(row.get("signal_key") or "")
        classification = _classify_signal(signal)
        if classification is None:
            continue
        diagnostic = _diagnostic_for_signal(row, classification)
        if classification["surface"] == "frontier":
            frontier.append(diagnostic)
            if "OKX_SPOT" in signal and float(row.get("avg_pnl_bps") or 0.0) > 0:
                positive_expansion.append(_positive_expansion_variant(row, classification))
        elif classification["surface"] == "global_proxy":
            yahoo.append(diagnostic)
        elif classification["surface"] == "perp_funding_basis":
            okx.append(diagnostic)

    frontier.sort(key=lambda item: (item["action_rank"], item["avg_pnl_bps"]))
    yahoo.sort(key=lambda item: (item["action_rank"], item["avg_pnl_bps"]))
    okx.sort(key=lambda item: (item["action_rank"], item["avg_pnl_bps"]))
    return {
        "paper_only": True,
        "active_loosenings_created": 0,
        "frontier_weak_signal_diagnostics": frontier[:30],
        "yahoo_proxy_diagnostics": yahoo[:20],
        "yahoo_proxy_decay_analysis": yahoo_decay_analysis,
        "okx_basis_funding_diagnostics": okx[:25],
        "positive_shadow_expansion_variants": positive_expansion[:20],
        "bucket_dimensions": {
            "frontier": [
                "venue",
                "direction",
                "quality_score",
                "source_count",
                "depth_adjusted_edge",
                "route_status",
                "quote_normalization",
                "cost_bucket",
            ],
            "yahoo_proxy": [
                "direction",
                "1d_momentum",
                "5d_momentum",
                "20d_momentum",
                "spread_bucket",
                "quote_staleness",
                "gap_bucket",
                "risk_context",
            ],
            "okx_basis_funding": [
                "route_status",
                "funding_sign",
                "funding_magnitude",
                "time_to_next_funding",
                "basis_slope",
                "spread",
                "cost_bucket",
                "horizon",
            ],
        },
        "hard_limits": [
            "Diagnostics and expansion variants are shadow-first.",
            "No active paper loosening happens without reliable outcome evidence.",
            "OKX basis mean-reversion and borrow-dependent reverse routes remain gated.",
        ],
    }


def build_open_pack_report(
    conn: sqlite3.Connection,
    settings: Mapping[str, Any],
    *,
    candidates: Iterable[Mapping[str, Any]] | None = None,
    prediction_summary: Mapping[str, Any] | None = None,
    expansion_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidates = list(candidates or [])
    report = {
        "generated_at": utc_now(),
        "paper_only": True,
        "live_trading_allowed": bool(settings.get("allow_live_trading", False)),
        "implementation_status": IMPLEMENTED_STATUS,
        "deduplicated_status": DEDUPED_STATUS,
        "covered_improvement_task_ids": list(COVERED_IMPROVEMENT_TASK_IDS),
        "covered_growth_experiment_ids": list(COVERED_GROWTH_EXPERIMENT_IDS),
        "deduplicated_growth_experiment_ids": list(DEDUPED_GROWTH_EXPERIMENT_IDS),
        "route_borrow_intelligence": build_spot_borrow_intelligence(candidates),
        "africa_rail_watchlist": build_africa_rail_watchlist(),
        "kalshi_public_coverage": build_kalshi_public_coverage(prediction_summary),
        "signal_repair_diagnostics": build_signal_repair_diagnostics(conn),
        "expansion_context": dict(expansion_map or {}),
        "hard_limits": [
            "No live trading.",
            "No credentials or account changes.",
            "No private broker, margin, route, or order APIs.",
            "No dependency installs, startup task changes, or destructive data operations.",
        ],
    }
    return report


def write_open_pack_reports(report: Mapping[str, Any]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_open_pack_markdown(report), encoding="utf-8")


def close_covered_open_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    """Close covered open rows by status while preserving audit history."""

    improvement_updated = _update_ids(
        conn,
        "improvement_tasks",
        COVERED_IMPROVEMENT_TASK_IDS,
        IMPLEMENTED_STATUS,
    )
    implemented_growth_ids = [item for item in COVERED_GROWTH_EXPERIMENT_IDS if item not in DEDUPED_GROWTH_EXPERIMENT_IDS]
    growth_updated = _update_ids(
        conn,
        "growth_experiments",
        implemented_growth_ids,
        IMPLEMENTED_STATUS,
    )
    growth_deduped = _update_ids(
        conn,
        "growth_experiments",
        DEDUPED_GROWTH_EXPERIMENT_IDS,
        DEDUPED_STATUS,
    )
    conn.commit()
    return {
        "improvement_tasks_updated": improvement_updated,
        "growth_experiments_updated": growth_updated,
        "growth_experiments_deduplicated": growth_deduped,
        "status": IMPLEMENTED_STATUS,
        "deduplicated_status": DEDUPED_STATUS,
    }


def render_open_pack_markdown(report: Mapping[str, Any]) -> str:
    borrow = report.get("route_borrow_intelligence") or {}
    africa = report.get("africa_rail_watchlist") or {}
    kalshi = report.get("kalshi_public_coverage") or {}
    diagnostics = report.get("signal_repair_diagnostics") or {}
    lines = [
        "# Self-Improvement Open Pack",
        "",
        "Consolidated paper-only implementation for the current open self-improvement queue.",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Paper only: `{report.get('paper_only')}`",
        f"- Live trading allowed: `{report.get('live_trading_allowed')}`",
        f"- Covered improvement tasks: `{report.get('covered_improvement_task_ids', [])}`",
        f"- Covered growth experiments: `{report.get('covered_growth_experiment_ids', [])}`",
        "",
        "## Route Borrow Intelligence",
        "",
        f"- Records: `{borrow.get('record_count', 0)}`",
        f"- Shadow-only unconfirmed: `{borrow.get('shadow_only_unconfirmed_count', 0)}`",
        f"- By venue: `{borrow.get('by_venue', {})}`",
        f"- By availability: `{borrow.get('by_borrow_availability', {})}`",
    ]
    for row in (borrow.get("records") or [])[:10]:
        lines.append(
            f"- `{row.get('venue')}` `{row.get('instrument')}` `{row.get('direction')}` "
            f"borrow=`{row.get('borrow_availability')}` shadow=`{row.get('shadow_only_until_confirmed')}` "
            f"source={row.get('doc_api_source_url')}"
        )

    lines.extend(
        [
            "",
            "## Africa Rail Watchlist",
            "",
            f"- Venues: `{africa.get('venue_count', 0)}`",
            f"- Instruments: `{africa.get('instrument_count', 0)}`",
            f"- Availability: `{africa.get('by_venue_availability', {})}`",
        ]
    )
    for row in (africa.get("rails") or [])[:8]:
        lines.append(
            f"- `{row.get('venue')}` fiat={row.get('fiat_currencies')} "
            f"status=`{row.get('venue_availability')}` instruments={row.get('watch_instruments')}"
        )

    lines.extend(
        [
            "",
            "## Kalshi Public Coverage",
            "",
            f"- Current candidates: `{kalshi.get('current_candidate_count', 0)}`",
            f"- Route status: `{kalshi.get('route_status')}`",
            f"- Orderbooks: `{kalshi.get('orderbook_status_counts', {})}`",
            f"- Route blockers: `{kalshi.get('route_blockers', {})}`",
            "",
            "## Signal Repair Diagnostics",
            "",
            f"- Active loosenings created: `{diagnostics.get('active_loosenings_created', 0)}`",
            f"- Frontier diagnostics: `{len(diagnostics.get('frontier_weak_signal_diagnostics', []))}`",
            f"- Yahoo diagnostics: `{len(diagnostics.get('yahoo_proxy_diagnostics', []))}`",
            f"- OKX diagnostics: `{len(diagnostics.get('okx_basis_funding_diagnostics', []))}`",
            f"- Positive shadow expansions: `{len(diagnostics.get('positive_shadow_expansion_variants', []))}`",
        ]
    )
    yahoo_decay = diagnostics.get("yahoo_proxy_decay_analysis") or {}
    if yahoo_decay:
        summary = yahoo_decay.get("localization_summary") or {}
        lines.append(
            f"- Yahoo decay localization: localized=`{summary.get('localized_decay_detected')}` "
            f"primary_horizon=`{yahoo_decay.get('primary_horizon_minutes')}` "
            f"sources=`{summary.get('likely_decay_sources', [])}`"
        )
    for row in (diagnostics.get("frontier_weak_signal_diagnostics") or [])[:10]:
        lines.append(
            f"- `{row.get('signal_key')}` action=`{row.get('recommended_action')}` "
            f"avg=`{row.get('avg_pnl_bps')}` win_rate=`{row.get('win_rate')}`"
        )
    lines.extend(["", "## Hard Limits", ""])
    for limit in report.get("hard_limits", []):
        lines.append(f"- {limit}")
    return "\n".join(lines) + "\n"


def _update_ids(conn: sqlite3.Connection, table: str, ids: Iterable[int], status: str) -> int:
    ids = list(ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"update {table} set status = ? where status = 'open' and id in ({placeholders})",
        (status, *ids),
    )
    return int(cur.rowcount or 0)


def _load_signal_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _classify_signal(signal_key: str) -> dict[str, str] | None:
    lowered = signal_key.lower()
    for venue, direction, surface in WEAK_SIGNAL_TARGETS:
        if venue.lower() not in lowered:
            continue
        if direction == "basis":
            if "basis" not in lowered and "funding" not in lowered:
                continue
        elif direction.lower() not in lowered:
            continue
        return {"venue": venue, "direction": direction, "surface": surface}
    if "okx_spot" in lowered and "frontier" in lowered:
        direction = "short" if "short" in lowered else "long" if "long" in lowered else "unknown"
        return {"venue": "OKX_SPOT", "direction": direction, "surface": "frontier"}
    return None


def _diagnostic_for_signal(row: Mapping[str, Any], classification: Mapping[str, str]) -> dict[str, Any]:
    avg = float(row.get("avg_pnl_bps") or 0.0)
    win_rate = float(row.get("win_rate") or 0.0)
    closed = int(row.get("closed_count") or 0)
    if closed < 5:
        action = "observe_low_sample_shadow_first"
        rank = 3
    elif avg < -20.0 or win_rate < 0.35:
        action = "diagnose_and_shadow_gate_until_reliable_recovery"
        rank = 0
    elif avg > 0.0 and win_rate >= 0.45:
        action = "shadow_expansion_only_through_promotion_gates"
        rank = 2
    else:
        action = "keep_context_diagnostics_active"
        rank = 1
    return {
        "signal_key": row.get("signal_key"),
        "venue": classification.get("venue"),
        "direction": classification.get("direction"),
        "surface": classification.get("surface"),
        "closed_count": closed,
        "avg_pnl_bps": round(avg, 3),
        "win_rate": round(win_rate, 3),
        "score_adjustment": row.get("score_adjustment"),
        "recommended_action": action,
        "action_rank": rank,
        "active_loosen": False,
        "diagnostic_buckets": _diagnostic_buckets(classification),
    }


def _positive_expansion_variant(row: Mapping[str, Any], classification: Mapping[str, str]) -> dict[str, Any]:
    return {
        "signal_key": row.get("signal_key"),
        "venue": classification.get("venue"),
        "direction": classification.get("direction"),
        "status": "shadow_only",
        "expansion_type": "bounded_positive_slice_probe",
        "promotion_required": "existing_reliable_label_gates",
        "reason": "positive historical slice gets exploration without active loosening",
    }


def _diagnostic_buckets(classification: Mapping[str, str]) -> list[str]:
    surface = classification.get("surface")
    if surface == "frontier":
        return [
            "venue",
            "direction",
            "quality_score",
            "source_count",
            "depth_adjusted_edge",
            "route_status",
            "quote_normalization",
            "cost_bucket",
        ]
    if surface == "global_proxy":
        return [
            "direction",
            "1d_momentum",
            "5d_momentum",
            "20d_momentum",
            "spread_bucket",
            "quote_staleness",
            "gap_bucket",
            "risk_context",
        ]
    return [
        "route_status",
        "funding_sign",
        "funding_magnitude",
        "time_to_next_funding",
        "basis_slope",
        "spread",
        "cost_bucket",
        "horizon",
    ]


def _route_blockers(candidate: Mapping[str, Any], route: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for field in ("route_blockers", "missing_requirements"):
        value = candidate.get(field)
        if value:
            values.append(value)
    for field in ("route_blockers", "missing_permissions"):
        value = route.get(field)
        if value:
            values.append(value)
    output: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts = value.replace("|", ",").split(",")
            output.extend(part.strip().strip("'\"[] ") for part in parts if part.strip())
        elif isinstance(value, Iterable):
            output.extend(str(item) for item in value if item)
    return list(dict.fromkeys(item for item in output if item))


def _venue(candidate: Mapping[str, Any]) -> str:
    venue = candidate.get("venue")
    if venue:
        return str(venue).upper()
    inst_id = str(candidate.get("inst_id") or "")
    if ":" in inst_id:
        return inst_id.split(":", 1)[0].upper()
    return "UNKNOWN"


def _base_asset(inst_id: str) -> str:
    raw = inst_id.split(":", 1)[-1]
    for sep in ("_", "-", "/"):
        if sep in raw:
            return raw.split(sep, 1)[0]
    return raw or "unknown"


def _first_number(candidate: Mapping[str, Any], *fields: str) -> float | None:
    for field in fields:
        value = candidate.get(field)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _borrow_record(
    candidate: Mapping[str, Any],
    route: Mapping[str, Any],
    venue: str,
    inst_id: str,
    direction: str,
    blockers: list[str],
) -> dict[str, Any]:
    borrow_fee = _first_number(candidate, "borrow_fee_bps_estimate", "borrow_fee_bps")
    edge = _first_number(candidate, "edge_bps_estimate", "net_edge_bps_estimate", "edge_bps")
    round_trip_cost = _first_number(candidate, "estimated_round_trip_cost_bps", "round_trip_cost_bps", "total_cost_bps")
    net_after_borrow = None
    if edge is not None:
        penalty = (borrow_fee or 0.0) + (round_trip_cost or 0.0)
        net_after_borrow = round(edge - penalty, 3)
    confirmed = "spot_borrow" not in blockers and str(route.get("borrow_status") or "").lower() == "configured"
    return {
        "venue": venue,
        "instrument": inst_id,
        "direction": direction or "unknown",
        "margin_or_short_support": "confirmed" if confirmed else "unconfirmed",
        "account_permission": "spot_borrow_or_margin_required" if "spot_borrow" in blockers else "not_required",
        "borrow_availability": "confirmed" if confirmed else "unknown_public_data_not_confirmed",
        "borrow_fee_bps_estimate": borrow_fee if borrow_fee is not None else "unknown",
        "fee_tier": candidate.get("fee_tier") or "unknown",
        "minimum_notional": candidate.get("minimum_notional") or "unknown",
        "borrow_asset": _base_asset(inst_id),
        "doc_api_source_url": BORROW_DOC_SOURCES.get(venue, "unknown"),
        "last_checked_at": route.get("last_checked_at") or utc_now(),
        "confidence": route.get("confidence") if route.get("confidence") is not None else 0.35,
        "route_status": route.get("route_status") or candidate.get("route_status") or "unknown",
        "route_blockers": blockers,
        "net_edge_after_borrow_cost_bps": net_after_borrow,
        "shadow_only_until_confirmed": not confirmed or (net_after_borrow is not None and net_after_borrow <= 0.0),
    }
