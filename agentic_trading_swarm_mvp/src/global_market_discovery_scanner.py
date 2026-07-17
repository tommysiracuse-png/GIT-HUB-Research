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
    """Load the most recent research-worker discoveries, falling back to seeds."""

    cfg = _cfg(settings)
    candidates: list[dict[str, Any]] = []
    report = _read_json(RESEARCH_REPORT_JSON) if RESEARCH_REPORT_JSON.exists() else {}
    for item in report.get("candidates", []) or []:
        candidates.append(dict(item))
    if not candidates and DISCOVERY_JSONL.exists():
        seen: set[str] = set()
        for line in DISCOVERY_JSONL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate_id = str(item.get("candidate_id") or "")
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
    return filtered[: int(cfg.get("max_surfaces_per_cycle", 10))]


def _proxy_map(settings: dict | None = None) -> dict[str, list[dict[str, Any]]]:
    configured = _cfg(settings).get("proxy_map")
    if isinstance(configured, dict):
        merged = {key: list(value) for key, value in DEFAULT_PROXY_MAP.items()}
        for venue, proxies in configured.items():
            if isinstance(proxies, list):
                merged[str(venue)] = [dict(item) for item in proxies if isinstance(item, dict)]
        return merged
    return DEFAULT_PROXY_MAP


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
    return discovery, {"symbol": symbol, "label": f"required open instrument {symbol}", "surface": "required_open_trade"}


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
            targets.append((discovery, proxy))
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


def _build_proxy_candidate(discovery: dict[str, Any], proxy: dict[str, Any], settings: dict | None = None) -> dict[str, Any] | None:
    symbol = str(proxy["symbol"])
    chart = fetch_chart(symbol)
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
    direction = "long_proxy" if ret_1d_bps >= 0 else "short_proxy"
    if stale_minutes > float(_cfg(settings).get("watch_only_stale_minutes", 180)):
        direction = "watch_only"

    surface_priority = float(discovery.get("priority") or 50.0)
    confidence = float(discovery.get("confidence") or 0.5)
    abs_signal = abs(ret_1d_bps) * 0.10 + abs(ret_short_bps) * 0.14
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

    return {
        "seen_at": _utc_now(),
        "venue": _venue_key(venue),
        "venue_display_name": venue,
        "inst_id": inst_id,
        "proxy_symbol": symbol,
        "proxy_label": proxy.get("label") or symbol,
        "proxy_surface": proxy.get("surface") or "global_proxy",
        "name": f"{venue} via {proxy.get('label') or symbol}",
        "region": discovery.get("region", "Global"),
        "country": discovery.get("country", "unknown"),
        "asset_class": discovery.get("surface_type_classified") or "global_market_proxy",
        "market_surface": "global_market_discovery",
        "trade_type": "global_market_discovery_proxy",
        "direction": direction,
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

    candidates.sort(key=lambda row: (row.get("direction") != "watch_only", float(row.get("score") or 0.0)), reverse=True)
    selected = candidates[:limit] if limit else candidates
    observations = [
        observation_from_candidate(candidate, source="global_market_discovery_scanner")
        for candidate in candidates
        if candidate.get("direction") != "watch_only"
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
