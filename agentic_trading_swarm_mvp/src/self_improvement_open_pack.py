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


def build_signal_repair_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    signal_rows = _load_signal_stats(conn)
    frontier = []
    yahoo = []
    okx = []
    positive_expansion = []
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
