#!/usr/bin/env python3
"""Paper scanner for global market-discovery surfaces.

The research worker finds broad market surfaces. This scanner turns the
priceable ones into normal radar candidates by using public/proxy instruments
that the current paper engine can already price. Unpriceable discoveries remain
watch-only evidence until an adapter exists.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import pathlib
import time
from typing import Any

from global_proxy_scanner import (
    bps_change,
    estimated_spread_bps,
    fetch_chart,
    liquidity_score,
    valid_pairs,
)
from research_worker import DEFAULT_GLOBAL_DISCOVERY_SEEDS, normalize_market_candidate
from scan_batch import ScanBatch, observation_from_candidate
from proxy_signal_quality import enrich_parsed_proxy_quality
from yahoo_proxy_reuse import evaluate_yahoo_proxy_reuse


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
RESEARCH_REPORT_JSON = RUNS_DIR / "research_worker_latest.json"
DISCOVERY_JSONL = RUNS_DIR / "market_discovery_candidates.jsonl"
REPORT_JSON = RUNS_DIR / "global_market_discovery_scan_latest.json"
REPORT_MD = RUNS_DIR / "global_market_discovery_scan_report.md"


DEFAULT_PROXY_MAP: dict[str, list[dict[str, Any]]] = {
    "Australian Securities Exchange": [
        {"symbol": "EWA", "label": "Australia ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "BHP", "label": "BHP ADR/global miner proxy", "surface": "local_equity_adr"},
        {"symbol": "RIO", "label": "Rio Tinto ADR/global miner proxy", "surface": "local_equity_adr"},
        {"symbol": "CBA.AX", "label": "Commonwealth Bank local equity proxy", "surface": "local_equity"},
    ],
    "B3": [
        {"symbol": "EWZ", "label": "Brazil ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "PBR", "label": "Petrobras ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "VALE", "label": "Vale ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "NU", "label": "Brazil/LatAm fintech proxy", "surface": "local_equity_adr"},
    ],
    "Bolsa Mexicana de Valores": [
        {"symbol": "EWW", "label": "Mexico ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "AMX", "label": "America Movil ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "CX", "label": "Cemex ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "KOF", "label": "Coca-Cola FEMSA ADR proxy", "surface": "local_equity_adr"},
    ],
    "CME Group": [
        {"symbol": "SPY", "label": "US equity-index ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "TLT", "label": "US rates ETF proxy", "surface": "rates_proxy"},
        {"symbol": "GLD", "label": "gold ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "USO", "label": "oil ETF proxy", "surface": "commodity_proxy"},
    ],
    "Cboe Global Markets": [
        {"symbol": "VIXY", "label": "short-term VIX futures ETF proxy", "surface": "volatility_proxy"},
        {"symbol": "VXX", "label": "VIX ETN proxy", "surface": "volatility_proxy"},
        {"symbol": "SVXY", "label": "short-volatility ETF proxy", "surface": "volatility_proxy"},
        {"symbol": "UVXY", "label": "levered VIX ETF proxy", "surface": "volatility_proxy"},
    ],
    "Eurex": [
        {"symbol": "EWG", "label": "Germany ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "FEZ", "label": "Eurozone equity ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "VGK", "label": "Europe equity ETF proxy", "surface": "equity_index_proxy"},
    ],
    "Euronext": [
        {"symbol": "FEZ", "label": "Eurozone equity ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "EWQ", "label": "France ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "EWN", "label": "Netherlands ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "EWK", "label": "Belgium ETF proxy", "surface": "equity_index_proxy"},
    ],
    "National Stock Exchange of India": [
        {"symbol": "INDA", "label": "India ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "HDB", "label": "HDFC Bank ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "IBN", "label": "ICICI Bank ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "INFY", "label": "Infosys ADR proxy", "surface": "local_equity_adr"},
    ],
    "Japan Exchange Group": [
        {"symbol": "EWJ", "label": "Japan ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "DXJ", "label": "hedged Japan ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "TM", "label": "Toyota ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "SONY", "label": "Sony ADR proxy", "surface": "local_equity_adr"},
    ],
    "Hong Kong Exchanges and Clearing": [
        {"symbol": "EWH", "label": "Hong Kong ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "FXI", "label": "China large-cap ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "BABA", "label": "Alibaba ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "JD", "label": "JD.com ADR proxy", "surface": "local_equity_adr"},
    ],
    "Intercontinental Exchange": [
        {"symbol": "USO", "label": "oil ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "UNG", "label": "natural gas ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "DBA", "label": "agriculture ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "WEAT", "label": "wheat ETF proxy", "surface": "commodity_proxy"},
    ],
    "Johannesburg Stock Exchange": [
        {"symbol": "EZA", "label": "South Africa ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "GOLD", "label": "Gold Fields ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "SBSW", "label": "Sibanye Stillwater ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "SSL", "label": "Sasol ADR proxy", "surface": "local_equity_adr"},
    ],
    "Korea Exchange": [
        {"symbol": "EWY", "label": "South Korea ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "PKX", "label": "POSCO ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "KB", "label": "KB Financial ADR proxy", "surface": "local_equity_adr"},
    ],
    "London Metal Exchange": [
        {"symbol": "DBB", "label": "base metals ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "CPER", "label": "copper ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "SLV", "label": "silver ETF proxy", "surface": "commodity_proxy"},
        {"symbol": "GLD", "label": "gold ETF proxy", "surface": "commodity_proxy"},
    ],
    "London Stock Exchange": [
        {"symbol": "EWU", "label": "UK ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "SHEL", "label": "Shell ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "HSBC", "label": "HSBC ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "AZN", "label": "AstraZeneca ADR proxy", "surface": "local_equity_adr"},
    ],
    "FINRA TRACE": [
        {"symbol": "HYG", "label": "high-yield credit ETF proxy", "surface": "credit_proxy"},
        {"symbol": "LQD", "label": "investment-grade credit ETF proxy", "surface": "credit_proxy"},
        {"symbol": "JNK", "label": "high-yield credit ETF proxy", "surface": "credit_proxy"},
        {"symbol": "EMB", "label": "emerging-market debt ETF proxy", "surface": "credit_proxy"},
    ],
    "Frankfurter/ECB reference FX": [
        {"symbol": "UUP", "label": "US dollar ETF proxy", "surface": "fx_proxy"},
        {"symbol": "FXE", "label": "euro ETF proxy", "surface": "fx_proxy"},
        {"symbol": "FXY", "label": "yen ETF proxy", "surface": "fx_proxy"},
    ],
    "Saudi Exchange": [
        {"symbol": "KSA", "label": "Saudi Arabia ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "UAE", "label": "UAE ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "QAT", "label": "Qatar ETF proxy", "surface": "equity_index_proxy"},
    ],
    "Singapore Exchange": [
        {"symbol": "EWS", "label": "Singapore ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "SE", "label": "Sea Ltd ADR/global Singapore proxy", "surface": "local_equity_adr"},
    ],
    "SIX Swiss Exchange": [
        {"symbol": "EWL", "label": "Switzerland ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "NVS", "label": "Novartis ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "RHHBY", "label": "Roche ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "NESN.SW", "label": "Nestle local equity proxy", "surface": "local_equity"},
    ],
    "Taiwan Stock Exchange": [
        {"symbol": "EWT", "label": "Taiwan ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "TSM", "label": "TSMC ADR proxy", "surface": "local_equity_adr"},
        {"symbol": "UMC", "label": "UMC ADR proxy", "surface": "local_equity_adr"},
    ],
    "TMX Group": [
        {"symbol": "EWC", "label": "Canada ETF proxy", "surface": "equity_index_proxy"},
        {"symbol": "RY", "label": "Royal Bank of Canada US listing proxy", "surface": "local_equity_adr"},
        {"symbol": "TD", "label": "Toronto-Dominion US listing proxy", "surface": "local_equity_adr"},
        {"symbol": "SHOP", "label": "Shopify US listing proxy", "surface": "local_equity_adr"},
    ],
}


ACTIVE_PAPER_COHORT: tuple[dict[str, str], ...] = (
    {"venue": "Cboe Global Markets", "symbol": "VIXY", "surface": "volatility_long", "region": "United States"},
    {"venue": "Cboe Global Markets", "symbol": "VXX", "surface": "volatility_long", "region": "United States"},
    {"venue": "Cboe Global Markets", "symbol": "UVXY", "surface": "volatility_long", "region": "United States"},
    {"venue": "Cboe Global Markets", "symbol": "SVXY", "surface": "volatility_short", "region": "United States"},
    {"venue": "Korea Exchange", "symbol": "EWY", "surface": "country_etf", "region": "South Korea"},
    {"venue": "Korea Exchange", "symbol": "PKX", "surface": "adr", "region": "South Korea"},
    {"venue": "Taiwan Stock Exchange", "symbol": "EWT", "surface": "country_etf", "region": "Taiwan"},
    {"venue": "Taiwan Stock Exchange", "symbol": "TSM", "surface": "adr", "region": "Taiwan"},
    {"venue": "Taiwan Stock Exchange", "symbol": "UMC", "surface": "adr", "region": "Taiwan"},
    {"venue": "B3", "symbol": "EWZ", "surface": "country_etf", "region": "Brazil"},
    {"venue": "B3", "symbol": "NU", "surface": "adr", "region": "Brazil"},
    {"venue": "TMX Group", "symbol": "SHOP", "surface": "adr", "region": "Canada"},
    {"venue": "TMX Group", "symbol": "TD", "surface": "adr", "region": "Canada"},
    {"venue": "TMX Group", "symbol": "RY", "surface": "adr", "region": "Canada"},
    {"venue": "CME Group", "symbol": "GLD", "surface": "precious_metal_proxy", "region": "Global"},
    {"venue": "London Metal Exchange", "symbol": "SLV", "surface": "precious_metal_proxy", "region": "Global"},
    {"venue": "CME Group", "symbol": "SPY", "surface": "equity_index_proxy", "region": "United States"},
    {"venue": "Japan Exchange Group", "symbol": "EWJ", "surface": "country_etf", "region": "Japan"},
    {"venue": "Australian Securities Exchange", "symbol": "BHP", "surface": "adr", "region": "Australia"},
)
ACTIVE_PAPER_SYMBOLS = {item["symbol"] for item in ACTIVE_PAPER_COHORT}
_CHART_CACHE: dict[tuple[int, str], dict[str, Any]] = {}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _cfg(settings: dict | None) -> dict:
    return (settings or {}).get("global_market_discovery_scanner", {})


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _discovery_identity(item: dict[str, Any]) -> str:
    if item.get("candidate_id"):
        return str(item["candidate_id"])
    normalized = normalize_market_candidate(item)
    return str(normalized.get("candidate_id") or "")


def _merge_default_discoveries(candidates: list[dict[str, Any]], settings: dict | None = None) -> list[dict[str, Any]]:
    if not bool(_cfg(settings).get("merge_default_seeds", True)):
        return candidates
    merged = list(candidates)
    seen = {_discovery_identity(item) for item in merged}
    for seed in DEFAULT_GLOBAL_DISCOVERY_SEEDS:
        normalized = normalize_market_candidate(seed)
        identity = str(normalized.get("candidate_id") or "")
        if identity and identity not in seen:
            merged.append(normalized)
            seen.add(identity)
    return merged


def load_discovery_candidates(settings: dict | None = None) -> list[dict[str, Any]]:
    """Load the durable discovery ledger plus the latest research-worker results."""

    cfg = _cfg(settings)
    candidates: list[dict[str, Any]] = []
    report = _read_json(RESEARCH_REPORT_JSON) if RESEARCH_REPORT_JSON.exists() else {}
    for item in report.get("candidates", []) or []:
        candidates.append(dict(item))
    seen: set[str] = {_discovery_identity(item) for item in candidates}
    if DISCOVERY_JSONL.exists():
        for line in DISCOVERY_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = normalize_market_candidate(item, created_at=str(item.get("created_at") or _utc_now()))
            candidate_id = _discovery_identity(item)
            if candidate_id and candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidates.append(item)
    if not candidates:
        candidates = [normalize_market_candidate(item) for item in DEFAULT_GLOBAL_DISCOVERY_SEEDS]
    else:
        candidates = _merge_default_discoveries(candidates, settings)

    min_priority = int(cfg.get("min_discovery_priority", 70))
    filtered = [item for item in candidates if int(item.get("priority") or 0) >= min_priority]
    filtered.sort(key=lambda row: (int(row.get("priority") or 0), float(row.get("confidence") or 0.0)), reverse=True)
    limit = int(cfg.get("max_surfaces_per_cycle", 10))
    rotation_slots = min(limit, max(0, int(cfg.get("recent_discovery_rotation_slots", 8))))
    recent = sorted(
        [item for item in filtered if item.get("discovered_by") == "openai_responses_web_search"],
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )[:rotation_slots]
    selected_ids = {_discovery_identity(item) for item in recent}
    remaining = [item for item in filtered if _discovery_identity(item) not in selected_ids]
    return [*recent, *remaining[: max(0, limit - len(recent))]]


def _proxy_map(settings: dict | None = None) -> dict[str, list[dict[str, Any]]]:
    configured = _cfg(settings).get("proxy_map")
    if isinstance(configured, dict):
        merged = {key: list(value) for key, value in DEFAULT_PROXY_MAP.items()}
        for venue, proxies in configured.items():
            if isinstance(proxies, list):
                merged[str(venue)] = [dict(item) for item in proxies if isinstance(item, dict)]
        return merged
    return DEFAULT_PROXY_MAP


def _paper_scope_instrument_family(execution_surface: str) -> str:
    surface = str(execution_surface or "").strip().lower()
    if not surface:
        return "unknown"
    if "adr" in surface:
        return "adr"
    if "proxy" in surface:
        return "proxy"
    if "equity" in surface:
        return "equity"
    if "volatility" in surface:
        return "volatility"
    if "metal" in surface or "commodity" in surface:
        return "commodity"
    if "credit" in surface:
        return "credit"
    if "fx" in surface:
        return "fx"
    return surface.replace(" ", "_")


def _paper_scope_direction_family(discovery: dict[str, Any], proxy: dict[str, Any], execution_surface: str) -> str:
    explicit = str(proxy.get("direction_family") or discovery.get("direction_family") or "").strip().lower()
    if explicit:
        return explicit.replace(" ", "_")
    surface = str(execution_surface or "").strip().lower()
    if "short" in surface and "long" not in surface:
        return "short_only"
    if "long" in surface and "short" not in surface:
        return "long_only"
    return "undirected"


def _paper_strategy_surface_scope(discovery: dict[str, Any], proxy: dict[str, Any]) -> dict[str, Any]:
    execution_surface = str(proxy.get("cohort_surface") or proxy.get("surface") or discovery.get("surface_type_classified") or "unknown")
    direction_family = _paper_scope_direction_family(discovery, proxy, execution_surface)
    scope = {
        "venue": _venue_key(str(discovery.get("venue_or_source") or "unknown")),
        "instrument_family": _paper_scope_instrument_family(execution_surface),
        "direction_family": direction_family,
        "execution_surface": execution_surface,
        "exact_match_required": True,
        "paper_only": True,
        "replay_policy": "exact_declared_surface_only",
        "scope_mismatch_action": "refuse_reuse",
        "validation_status": "paper_invalid_until_fresh_target_surface_validation",
    }
    scope["scope_key"] = "|".join(
        [
            scope["venue"],
            scope["instrument_family"],
            scope["direction_family"],
            scope["execution_surface"],
        ]
    )
    return scope


def _decorate_target_surface_scope(discovery: dict[str, Any], proxy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = _paper_strategy_surface_scope(discovery, proxy)
    return (
        {**discovery, "paper_strategy_surface_scope": scope, "paper_strategy_surface_scope_key": scope["scope_key"]},
        {**proxy, "paper_strategy_surface_scope": scope, "paper_strategy_surface_scope_key": scope["scope_key"]},
    )


def _required_target(inst_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if ":" not in inst_id:
        return None
    venue, symbol = inst_id.split(":", 1)
    if not venue or not symbol:
        return None
    discovery = normalize_market_candidate(
        {
            "surface_type_raw": "required open global paper proxy",
            "surface_type_classified": "equity_or_proxy",
            "venue_or_source": venue,
            "country": "unknown",
            "region": "Global",
            "asset_or_event": f"Open paper proxy instrument {symbol}",
            "data_access_type": "public_no_key",
            "tradability_guess": "route_needed",
            "public_docs_url": "",
            "source_urls": [],
            "why_interesting": "Open paper trade requires complete repricing even if the surface is not selected this cycle.",
            "inefficiency_hypothesis": "Maintain reliable horizon labels for global discovery paper trades.",
            "latency_sensitivity": "medium",
            "liquidity_hint": "existing open paper instrument",
            "route_blockers": ["broker_route"],
            "recommended_next_action": "watchlist",
            "priority": 100,
            "confidence": 1.0,
        }
    )
    return _decorate_target_surface_scope(
        discovery,
        {"symbol": symbol, "label": f"required open instrument {symbol}", "surface": "required_open_trade"},
    )


def _build_targets(
    discoveries: list[dict[str, Any]],
    settings: dict | None = None,
    required_inst_ids: set[str] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    proxies_by_venue = _proxy_map(settings)
    cfg = _cfg(settings)
    max_per_surface = int(cfg.get("max_proxy_symbols_per_surface", 4))
    targets: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_inst_ids: set[str] = set()

    for discovery in discoveries:
        venue = str(discovery.get("venue_or_source") or "unknown")
        proxies = proxies_by_venue.get(venue, [])[:max_per_surface]
        for proxy in proxies:
            symbol = str(proxy.get("symbol") or "").strip()
            if not symbol:
                continue
            inst_id = f"{_venue_key(venue)}:{symbol}"
            if inst_id in seen_inst_ids:
                continue
            targets.append(_decorate_target_surface_scope(discovery, proxy))
            seen_inst_ids.add(inst_id)

    for cohort in ACTIVE_PAPER_COHORT if bool(cfg.get("active_paper_cohort_enabled", False)) else ():
        venue = cohort["venue"]
        symbol = cohort["symbol"]
        inst_id = f"{_venue_key(venue)}:{symbol}"
        if inst_id in seen_inst_ids:
            for index, (discovery, proxy) in enumerate(targets):
                if f"{_venue_key(str(discovery.get('venue_or_source') or 'unknown'))}:{proxy.get('symbol')}" == inst_id:
                    targets[index] = _decorate_target_surface_scope(
                        discovery,
                        {
                            **proxy,
                            "active_paper_cohort": True,
                            "cohort_surface": cohort["surface"],
                        },
                    )
                    break
            else:
                    break
            continue
        discovery = normalize_market_candidate(
            {
                "surface_type_raw": f"active paper {cohort['surface']}",
                "surface_type_classified": cohort["surface"],
                "venue_or_source": venue,
                "country": cohort["region"],
                "region": cohort["region"],
                "asset_or_event": symbol,
                "data_access_type": "public_no_key",
                "tradability_guess": "directly_tradable",
                "why_interesting": "High-priority active-paper cohort selected from system market-expansion evidence.",
                "inefficiency_hypothesis": "Surface-aware momentum and relative confirmation may identify short-horizon paper opportunities.",
                "latency_sensitivity": "medium",
                "liquidity_hint": "listed proxy or ADR",
                "route_blockers": [],
                "recommended_next_action": "growth_experiment",
                "priority": 95,
                "confidence": 0.8,
            }
        )
        targets.append(
            _decorate_target_surface_scope(
                discovery,
                {
                    "symbol": symbol,
                    "label": f"{symbol} active-paper cohort",
                    "surface": cohort["surface"],
                    "cohort_surface": cohort["surface"],
                    "active_paper_cohort": True,
                },
            )
        )
        seen_inst_ids.add(inst_id)

    for inst_id in sorted(required_inst_ids or set()):
        target = _required_target(inst_id)
        if target and inst_id not in seen_inst_ids:
            targets.append(target)
            seen_inst_ids.add(inst_id)
    return targets


def _venue_key(venue: str) -> str:
    return (
        venue.upper()
        .replace(" ", "_")
        .replace("/", "_")
        .replace(".", "")
        .replace("-", "_")
    )


def _stale_minutes(last_seen: dt.datetime) -> float:
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - last_seen).total_seconds() / 60.0)


def _chart_last_time(chart: dict[str, Any]) -> dt.datetime:
    timestamps = chart.get("timestamp") or []
    if timestamps:
        return dt.datetime.fromtimestamp(int(timestamps[-1]), tz=dt.timezone.utc)
    return dt.datetime.fromtimestamp(int(time.time()), tz=dt.timezone.utc)


def _cached_chart(symbol: str) -> dict[str, Any]:
    key = (id(fetch_chart), symbol)
    if key not in _CHART_CACHE:
        if len(_CHART_CACHE) >= 256:
            _CHART_CACHE.clear()
        _CHART_CACHE[key] = fetch_chart(symbol)
    return _CHART_CACHE[key]


def _chart_returns(chart: dict[str, Any]) -> tuple[float, float]:
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    pairs = valid_pairs(quote.get("close") or [], quote.get("volume") or [])
    if len(pairs) < 12:
        raise ValueError("insufficient_chart_history")
    last = pairs[-1][0]
    ref_1d = pairs[-27][0] if len(pairs) >= 27 else pairs[0][0]
    ref_short = pairs[-5][0] if len(pairs) >= 5 else pairs[0][0]
    return bps_change(last, ref_1d), bps_change(last, ref_short)


def _market_session(meta: dict[str, Any], last_seen: dt.datetime) -> str:
    market_state = str(meta.get("marketState") or "").lower()
    if market_state in {"regular", "open"}:
        return "open"
    if market_state in {"closed", "post", "pre", "postpost"}:
        return "closed"
    regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    try:
        start = float(regular.get("start"))
        end = float(regular.get("end"))
    except (TypeError, ValueError):
        start = end = 0.0
    if start > 0 and end > start:
        return "open" if start <= now_ts <= end else "closed"
    weekday = dt.datetime.now(dt.timezone.utc).weekday()
    if weekday >= 5 or _stale_minutes(last_seen) > 180.0:
        return "closed"
    return "unknown"


def _surface_signal(
    symbol: str,
    surface: str,
    ret_1d_bps: float,
    ret_short_bps: float,
) -> dict[str, Any]:
    if symbol == "SPY":
        aligned = ret_1d_bps * ret_short_bps > 0
        direction = "long_proxy" if ret_short_bps > 0 else "short_proxy"
        return {
            "direction": direction if aligned else "watch_only",
            "strength_bps": abs(ret_1d_bps) * 0.08 + abs(ret_short_bps) * 0.18,
            "variant": "spy_aligned_trend_v1",
            "reason": None if aligned else "short_and_daily_trend_not_aligned",
            "benchmark": {},
        }

    spy_1d, spy_short = _chart_returns(_cached_chart("SPY"))
    relative_1d = ret_1d_bps - spy_1d
    relative_short = ret_short_bps - spy_short
    direction = "watch_only"
    reason = "surface_confirmation_missing"
    variant = "country_adr_relative_momentum_v1"
    aligned_up = ret_1d_bps > 0 and ret_short_bps > 0
    aligned_down = ret_1d_bps < 0 and ret_short_bps < 0
    if surface == "volatility_long":
        variant = "long_vol_spy_shock_confirmation_v1"
        if aligned_up and spy_short < 0:
            direction, reason = "long_proxy", None
        elif aligned_down and spy_short > 0:
            direction, reason = "short_proxy", None
    elif surface == "volatility_short":
        variant = "short_vol_spy_regime_confirmation_v1"
        if aligned_up and spy_short >= 0:
            direction, reason = "long_proxy", None
        elif aligned_down and spy_short < 0:
            direction, reason = "short_proxy", None
    elif surface == "precious_metal_proxy":
        variant = "precious_metal_relative_momentum_v1"
        if aligned_up and relative_short > 0:
            direction, reason = "long_proxy", None
        elif aligned_down and relative_short < 0:
            direction, reason = "short_proxy", None
        try:
            uup_1d, uup_short = _chart_returns(_cached_chart("UUP"))
        except Exception:  # noqa: BLE001 - dollar confirmation is additive, not a hard dependency.
            uup_1d = uup_short = None
        benchmark = {
            "spy_return_1d_bps": round(spy_1d, 3),
            "spy_return_short_bps": round(spy_short, 3),
            "relative_1d_bps": round(relative_1d, 3),
            "relative_short_bps": round(relative_short, 3),
            "uup_return_1d_bps": round(uup_1d, 3) if uup_1d is not None else None,
            "uup_return_short_bps": round(uup_short, 3) if uup_short is not None else None,
        }
        return {
            "direction": direction,
            "strength_bps": abs(ret_short_bps) * 0.14 + abs(relative_short) * 0.12,
            "variant": variant,
            "reason": reason,
            "benchmark": benchmark,
        }
    else:
        if aligned_up and relative_short > 0:
            direction, reason = "long_proxy", None
        elif aligned_down and relative_short < 0:
            direction, reason = "short_proxy", None
    return {
        "direction": direction,
        "strength_bps": abs(ret_short_bps) * 0.14 + abs(relative_short) * 0.10 + abs(ret_1d_bps) * 0.05,
        "variant": variant,
        "reason": reason,
        "benchmark": {
            "spy_return_1d_bps": round(spy_1d, 3),
            "spy_return_short_bps": round(spy_short, 3),
            "relative_1d_bps": round(relative_1d, 3),
            "relative_short_bps": round(relative_short, 3),
        },
    }


def _build_proxy_candidate(discovery: dict[str, Any], proxy: dict[str, Any], settings: dict | None = None) -> dict[str, Any] | None:
    symbol = str(proxy["symbol"])
    chart = _cached_chart(symbol)
    reuse_gate = evaluate_yahoo_proxy_reuse(chart, settings)
    meta = chart.get("meta", {})
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    pairs = valid_pairs(quote.get("close") or [], quote.get("volume") or [])
    if len(pairs) < 12:
        return None

    last = pairs[-1][0]
    ref_1d = pairs[-27][0] if len(pairs) >= 27 else pairs[0][0]
    ref_short = pairs[-5][0] if len(pairs) >= 5 else pairs[0][0]
    ret_1d_bps = bps_change(last, ref_1d)
    ret_short_bps = bps_change(last, ref_short)
    recent_dollar_volume = sum(price * volume for price, volume in pairs[-27:])
    liq = liquidity_score(recent_dollar_volume)
    spread = estimated_spread_bps(liq)
    last_seen = _chart_last_time(chart)
    stale_minutes = _stale_minutes(last_seen)
    session_status = _market_session(meta, last_seen)
    active_cohort = bool(proxy.get("active_paper_cohort"))
    if active_cohort:
        surface_signal = _surface_signal(symbol, str(proxy.get("cohort_surface") or proxy.get("surface") or "proxy"), ret_1d_bps, ret_short_bps)
        direction = str(surface_signal["direction"])
        signal_strength = float(surface_signal["strength_bps"])
        strategy_variant = str(surface_signal["variant"])
        reject_reason = surface_signal.get("reason")
        benchmark_context = surface_signal.get("benchmark") or {}
    else:
        direction = "long_proxy" if ret_1d_bps >= 0 else "short_proxy"
        signal_strength = abs(ret_1d_bps) * 0.10 + abs(ret_short_bps) * 0.14
        strategy_variant = "legacy_global_discovery_momentum_v1"
        reject_reason = None
        benchmark_context = {}
    if not reuse_gate["proxy_valid_for_reuse"]:
        direction = "watch_only"
        reject_reason = f"proxy_invalid_for_reuse:{reuse_gate['reason'] or 'unknown'}"
    elif session_status == "closed":
        direction = "watch_only"
        reject_reason = "market_closed"
    elif stale_minutes > float(_cfg(settings).get("watch_only_stale_minutes", 180)):
        direction = "watch_only"
        reject_reason = "stale_market_data"

    surface_priority = float(discovery.get("priority") or 50.0)
    confidence = float(discovery.get("confidence") or 0.5)
    abs_signal = signal_strength
    edge_bps = round(max(0.0, min(abs_signal, 55.0) - spread), 3)
    discovery_boost = max(0.0, (surface_priority - 70.0) * 0.45) + confidence * 8.0
    score = round(
        max(0.0, min(100.0, abs_signal + liq * 24.0 + discovery_boost - spread - min(stale_minutes / 8.0, 18.0))),
        3,
    )
    risk = (settings or {}).get("risk", {})
    if (
        direction != "watch_only"
        and edge_bps >= float(risk.get("min_net_edge_bps", 2.0))
        and liq >= float(risk.get("min_liquidity_score", 0.35))
        and spread <= float(risk.get("max_spread_bps", 8.0))
        and stale_minutes <= 90.0
    ):
        score = max(score, float(_cfg(settings).get("priceable_candidate_score_floor", 42.0)))
    venue = str(discovery.get("venue_or_source") or "unknown")
    inst_id = f"{_venue_key(venue)}:{symbol}"
    source_urls = discovery.get("source_urls") or []
    source_url = source_urls[0] if source_urls else discovery.get("public_docs_url")

    candidate = {
        "seen_at": _utc_now(),
        "venue": _venue_key(venue),
        "venue_display_name": venue,
        "inst_id": inst_id,
        "proxy_symbol": symbol,
        "proxy_label": proxy.get("label") or symbol,
        "proxy_surface": proxy.get("surface") or "global_proxy",
        "active_paper_cohort": active_cohort,
        "strategy_variant": strategy_variant,
        "signal_lineage_key": f"GLOBAL_ACTIVE|{proxy.get('cohort_surface') or proxy.get('surface') or 'proxy'}|{strategy_variant}",
        "name": f"{venue} via {proxy.get('label') or symbol}",
        "region": discovery.get("region", "Global"),
        "country": discovery.get("country", "unknown"),
        "asset_class": discovery.get("surface_type_classified") or "global_market_proxy",
        "market_surface": "global_market_discovery",
        "trade_type": "global_market_discovery_proxy",
        "direction": direction,
        "candidate_reject_reason": reject_reason,
        "thesis": discovery.get("inefficiency_hypothesis") or discovery.get("why_interesting") or "global market discovery proxy signal",
        "last": round(last, 8),
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "edge_bps_estimate": edge_bps,
        "change_24h_pct": round(ret_1d_bps / 100.0, 3),
        "short_return_pct": round(ret_short_bps / 100.0, 3),
        "quote_volume_24h": round(recent_dollar_volume, 2),
        "liquidity_score": round(liq, 3),
        "spread_bps": round(spread, 3),
        "last_bar_utc": last_seen.isoformat(),
        "stale_minutes": round(stale_minutes, 1),
        "session_status": session_status,
        "max_entry_stale_minutes": 90.0,
        "proxy_quality_status": "verified_proxy" if session_status in {"open", "unknown"} and stale_minutes <= 90.0 else "unavailable",
        "proxy_valid_for_reuse": reuse_gate["proxy_valid_for_reuse"],
        "proxy_reuse_gate": reuse_gate,
        "benchmark_context": benchmark_context,
        "score": score,
        "market_key": f"global_discovery|{_venue_key(venue)}",
        "discovery_candidate_id": discovery.get("candidate_id"),
        "discovery_priority": int(discovery.get("priority") or 0),
        "discovery_confidence": round(confidence, 3),
        "surface_type_raw": discovery.get("surface_type_raw"),
        "surface_type_classified": discovery.get("surface_type_classified"),
        "recommended_next_action": discovery.get("recommended_next_action"),
        "route_blockers": discovery.get("route_blockers", []),
        "risk_notes": [
            "paper-trade only",
            "uses a public Yahoo-priced proxy for the discovered global market surface",
            "proxy may not perfectly track the local market, futures curve, credit tape, or event surface",
            "live execution remains disabled and route-specific permissions are not inferred",
        ],
        "data_source": {
            "provider": "Yahoo Finance chart endpoint via global market discovery scanner",
            "symbol": meta.get("symbol", symbol),
            "exchange": meta.get("exchangeName"),
            "currency": meta.get("currency"),
            "source_url": source_url,
            "public_docs_url": discovery.get("public_docs_url"),
        },
    }
    return enrich_parsed_proxy_quality(candidate)


def _watch_only_candidate(discovery: dict[str, Any]) -> dict[str, Any]:
    venue = str(discovery.get("venue_or_source") or "unknown")
    return {
        "seen_at": _utc_now(),
        "venue": _venue_key(venue),
        "venue_display_name": venue,
        "inst_id": f"{_venue_key(venue)}:{discovery.get('candidate_id', 'watch')}",
        "name": f"{venue} watch-only global discovery",
        "region": discovery.get("region", "Global"),
        "country": discovery.get("country", "unknown"),
        "asset_class": discovery.get("surface_type_classified") or "unknown_global_surface",
        "market_surface": "global_market_discovery",
        "trade_type": "global_market_discovery_proxy",
        "direction": "watch_only",
        "thesis": discovery.get("inefficiency_hypothesis") or discovery.get("why_interesting") or "global market discovery needs adapter/route work",
        "last": 1.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "edge_bps_estimate": 0.0,
        "change_24h_pct": 0.0,
        "short_return_pct": 0.0,
        "quote_volume_24h": 0.0,
        "liquidity_score": 0.0,
        "spread_bps": 999.0,
        "stale_minutes": 999.0,
        "score": min(35.0, float(discovery.get("priority") or 0.0) / 3.0),
        "market_key": f"global_discovery|{_venue_key(venue)}",
        "discovery_candidate_id": discovery.get("candidate_id"),
        "discovery_priority": int(discovery.get("priority") or 0),
        "discovery_confidence": float(discovery.get("confidence") or 0.0),
        "surface_type_raw": discovery.get("surface_type_raw"),
        "surface_type_classified": discovery.get("surface_type_classified"),
        "recommended_next_action": discovery.get("recommended_next_action"),
        "route_blockers": discovery.get("route_blockers", []),
        "candidate_reject_reason": "global_discovery_unpriced_watch_only",
        "risk_notes": [
            "watch-only global discovery",
            "no current public/proxy price mapping exists in this scanner",
            "needs adapter, route, or proxy mapping before paper entries are allowed",
        ],
        "data_source": {
            "provider": "global market discovery worker",
            "public_docs_url": discovery.get("public_docs_url"),
            "source_urls": discovery.get("source_urls", []),
        },
    }


def _summarize(candidates: list[dict[str, Any]], selected: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    by_venue: dict[str, int] = {}
    by_region: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for item in candidates:
        by_venue[item["venue"]] = by_venue.get(item["venue"], 0) + 1
        by_region[str(item.get("region") or "unknown")] = by_region.get(str(item.get("region") or "unknown"), 0) + 1
        by_direction[item["direction"]] = by_direction.get(item["direction"], 0) + 1
    return {
        "generated_at": _utc_now(),
        "total_candidates": len(candidates),
        "selected_candidates": len(selected),
        "priceable_candidates": sum(1 for item in candidates if item["direction"] != "watch_only"),
        "watch_only_candidates": sum(1 for item in candidates if item["direction"] == "watch_only"),
        "failed_proxy_fetches": len(failures),
        "by_venue": by_venue,
        "by_region": by_region,
        "by_direction": by_direction,
        "active_paper_cohort": [
            {
                "symbol": item.get("proxy_symbol"),
                "inst_id": item.get("inst_id"),
                "surface": item.get("proxy_surface"),
                "strategy_variant": item.get("strategy_variant"),
                "session_status": item.get("session_status"),
                "direction": item.get("direction"),
                "reject_reason": item.get("candidate_reject_reason"),
                "edge_bps_estimate": item.get("edge_bps_estimate"),
            }
            for item in candidates
            if item.get("active_paper_cohort")
        ],
        "top_candidates": [
            {
                "inst_id": item.get("inst_id"),
                "venue": item.get("venue"),
                "direction": item.get("direction"),
                "score": item.get("score"),
                "edge_bps_estimate": item.get("edge_bps_estimate"),
                "proxy_symbol": item.get("proxy_symbol"),
                "discovery_priority": item.get("discovery_priority"),
            }
            for item in selected[:20]
        ],
        "failures": failures[:20],
    }


def _write_report(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "mode": "paper_only_global_discovery_proxy",
        "hard_limits": [
            "No live trading.",
            "No credentials.",
            "No broker/order API calls.",
            "Proxy-priced surfaces are research/paper signals only.",
        ],
        "candidates": candidates[:100],
    }
    REPORT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Global Market Discovery Scan",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Total candidates: `{summary.get('total_candidates')}`",
        f"- Selected candidates: `{summary.get('selected_candidates')}`",
        f"- Priceable candidates: `{summary.get('priceable_candidates')}`",
        f"- Watch-only candidates: `{summary.get('watch_only_candidates')}`",
        f"- Failed proxy fetches: `{summary.get('failed_proxy_fetches')}`",
        f"- By venue: `{summary.get('by_venue', {})}`",
        f"- By region: `{summary.get('by_region', {})}`",
        "",
        "## Top Candidates",
        "",
    ]
    for item in summary.get("top_candidates", [])[:20]:
        lines.append(
            f"- `{item.get('inst_id')}` {item.get('direction')} score=`{item.get('score')}` "
            f"edge=`{item.get('edge_bps_estimate')}` proxy=`{item.get('proxy_symbol')}` "
            f"discovery_priority=`{item.get('discovery_priority')}`"
        )
    if summary.get("failures"):
        lines.extend(["", "## Proxy Fetch Failures", ""])
        for failure in summary.get("failures", [])[:20]:
            lines.append(f"- `{failure.get('venue')}` `{failure.get('symbol')}`: {failure.get('error')}")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_scan_batch(
    settings: dict,
    limit: int | None = None,
    required_inst_ids: set[str] | None = None,
) -> ScanBatch:
    discoveries = load_discovery_candidates(settings)
    targets = _build_targets(discoveries, settings, required_inst_ids=required_inst_ids)
    cfg = _cfg(settings)
    failures: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    priced_discovery_ids: set[str] = set()

    workers = int(cfg.get("workers", 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_to_target = {
            pool.submit(_build_proxy_candidate, discovery, proxy, settings): (discovery, proxy)
            for discovery, proxy in targets
        }
        for future in concurrent.futures.as_completed(future_to_target):
            discovery, proxy = future_to_target[future]
            try:
                candidate = future.result()
            except Exception as exc:  # noqa: BLE001 - report and continue; scanner must not kill radar loop.
                failures.append(
                    {
                        "venue": discovery.get("venue_or_source"),
                        "symbol": proxy.get("symbol"),
                        "error": str(exc)[:200],
                    }
                )
                candidate = None
            if candidate:
                candidates.append(candidate)
                if candidate.get("discovery_candidate_id"):
                    priced_discovery_ids.add(str(candidate["discovery_candidate_id"]))

    proxies_by_venue = _proxy_map(settings)
    for discovery in discoveries:
        discovery_id = str(discovery.get("candidate_id") or "")
        venue = str(discovery.get("venue_or_source") or "unknown")
        if discovery_id in priced_discovery_ids:
            continue
        if proxies_by_venue.get(venue):
            continue
        candidates.append(_watch_only_candidate(discovery))

    candidates.sort(
        key=lambda row: (
            bool(row.get("active_paper_cohort")),
            row.get("direction") != "watch_only",
            float(row.get("score") or 0.0),
        ),
        reverse=True,
    )
    if limit:
        cohort = [row for row in candidates if row.get("active_paper_cohort")]
        others = [row for row in candidates if not row.get("active_paper_cohort")]
        selected = [*cohort, *others[: max(0, int(limit) - len(cohort))]]
    else:
        selected = candidates
    observations = [
        observation_from_candidate(candidate, source="global_market_discovery_scanner")
        for candidate in candidates
        if candidate.get("proxy_symbol") and float(candidate.get("last") or 0.0) > 0.0
    ]
    summary = _summarize(candidates, selected, failures)
    _write_report(summary, candidates)
    return ScanBatch(
        source="global_market_discovery_scanner",
        candidates=selected,
        observations=observations,
        metadata={"global_market_discovery_scan": summary},
    )


def build_candidates(settings: dict, limit: int | None = None) -> list[dict[str, Any]]:
    return build_scan_batch(settings, limit=limit).candidates
