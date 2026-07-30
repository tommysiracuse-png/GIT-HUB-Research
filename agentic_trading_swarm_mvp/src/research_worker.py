#!/usr/bin/env python3
"""Global market discovery worker with provenance capture.

The worker is intentionally read-only with respect to external markets. It can
discover and rank market surfaces, then create internal paper/research artifacts
that the rest of the system can validate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from typing import Any, Iterable

from storage import (
    RUNS_DIR,
    add_adapter_spec,
    add_growth_experiment,
    add_hunter_directive,
    add_route_probe_task,
    connect,
)


REPORT_JSON = RUNS_DIR / "research_worker_latest.json"
REPORT_MD = RUNS_DIR / "research_worker_report.md"
CANDIDATES_JSONL = RUNS_DIR / "market_discovery_candidates.jsonl"
IMPLEMENTED_GLOBAL_DISCOVERY_STATUS = "implemented_global_market_discovery_scan"

DATA_ACCESS_TYPES = {"public_no_key", "public_key_required", "broker_account", "paid_data", "unknown"}
TRADABILITY_GUESSES = {"directly_tradable", "route_needed", "watch_only", "unknown"}
NEXT_ACTIONS = {"adapter_spec", "route_probe", "growth_experiment", "hunter_directive", "watchlist", "ignore"}

DEFAULT_GLOBAL_DISCOVERY_SEEDS: list[dict[str, Any]] = [
    {
        "surface_type_raw": "crypto global spot public market data",
        "venue_or_source": "Bybit",
        "country": "Global",
        "region": "Global",
        "asset_or_event": "BTCUSDT and other major spot books/tickers via public V5 spot endpoints",
        "data_access_type": "public_no_key",
        "tradability_guess": "directly_tradable",
        "public_docs_url": "https://bybit-exchange.github.io/docs/v5/market/tickers",
        "source_urls": [
            "https://bybit-exchange.github.io/docs/v5/market/tickers",
            "https://bybit-exchange.github.io/docs/v5/market/orderbook",
        ],
        "adapter_route_id": "bybit_perp_public",
        "adapter_request_hint": {
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
            "response_fields": ["result.list[0].lastPrice", "result.list[0].bid1Price", "result.list[0].ask1Price"],
        },
        "why_interesting": "Bybit already shows promising frontier paper behavior, but the current linear public ticker route can return 403 and leave scanner coverage degraded.",
        "inefficiency_hypothesis": "Switching paper discovery and venue-health reads to the public V5 spot ticker/book request shape can restore observability for an otherwise strong venue without adding any live execution scope.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "major global spot books",
        "route_blockers": ["public_endpoint_selection"],
        "recommended_next_action": "adapter_spec",
        "priority": 89,
        "confidence": 0.83,
    },
    {
        "surface_type_raw": "crypto regional fiat spot rails",
        "venue_or_source": "Bitso",
        "country": "Mexico",
        "region": "LATAM",
        "asset_or_event": "MXN, ARS, BRL and stablecoin crypto spot pairs",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://docs.bitso.com/bitso-api/docs",
        "source_urls": ["https://docs.bitso.com/bitso-api/docs"],
        "why_interesting": "Regional fiat/stablecoin books can diverge from global USD references.",
        "inefficiency_hypothesis": "Local fiat funding, capital controls, and exchange fragmentation may create persistent dislocations.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional exchange order books",
        "route_blockers": ["venue_api_access", "local_fiat_rail"],
        "recommended_next_action": "adapter_spec",
        "priority": 88,
        "confidence": 0.76,
    },
    {
        "surface_type_raw": "crypto regional fiat spot rails",
        "venue_or_source": "VALR",
        "country": "South Africa",
        "region": "Africa",
        "asset_or_event": "ZAR crypto spot and stablecoin markets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://docs.valr.com/",
        "source_urls": ["https://docs.valr.com/"],
        "why_interesting": "Africa fiat crypto markets are fragmented and already align with the system's frontier thesis.",
        "inefficiency_hypothesis": "Local on/off-ramp constraints and thinner depth can create price gaps against global USDT markets.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional order books",
        "route_blockers": ["venue_api_access", "local_fiat_rail"],
        "recommended_next_action": "adapter_spec",
        "priority": 86,
        "confidence": 0.74,
    },
    {
        "surface_type_raw": "crypto regional fiat spot rails",
        "venue_or_source": "Luno",
        "country": "Multiple",
        "region": "Africa/Asia",
        "asset_or_event": "NGN, ZAR, MYR and IDR crypto markets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.luno.com/en/developers/api",
        "source_urls": ["https://www.luno.com/en/developers/api"],
        "why_interesting": "Luno spans several regional fiat markets that are under-tested by the scanner.",
        "inefficiency_hypothesis": "Regional quote normalization may reveal fiat/stablecoin dislocations.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional exchange books",
        "route_blockers": ["venue_api_access", "local_fiat_rail"],
        "recommended_next_action": "adapter_spec",
        "priority": 84,
        "confidence": 0.72,
    },
    {
        "surface_type_raw": "cash equity benchmark and top-liquid constituent public market data",
        "venue_or_source": "Johannesburg Stock Exchange",
        "country": "South Africa",
        "region": "Africa",
        "asset_or_event": "FTSE/JSE Top 40, FTSE/JSE All Share, or top liquid cash equities",
        "data_access_type": "public_no_key",
        "tradability_guess": "watch_only",
        "public_docs_url": "https://www.jse.co.za/market-data",
        "source_urls": [
            "https://www.jse.co.za/market-data",
            "https://www.jse.co.za/indices",
        ],
        "adapter_route_id": "jse_cash_public_shadow",
        "adapter_request_hint": {
            "paper_only": True,
            "shadow_mode": {
                "enabled": True,
                "baseline": "proxy_generated_jse_candidates",
                "comparison_goal": "compare venue-native JSE discovery candidates against current proxy-generated JSE ideas before any broader frontier-equity rollout",
            },
            "scope_limit": "benchmark_or_top_liquid_names_only",
            "normalized_fields": [
                "venue",
                "symbol",
                "venue_native_symbol",
                "last_price",
                "timestamp",
                "session_timestamp",
                "freshness_minutes",
                "turnover_proxy",
                "volume_proxy",
                "spread_bps",
                "quote_quality",
                "liquidity_flags",
                "quality_flags",
                "top_mover_rank",
                "route_readiness",
            ],
            "preferred_symbols": ["J200", "J203", "AGL", "NPN", "SOL"],
            "top_mover_discovery": {
                "enabled": True,
                "limit": 10,
                "sort_fields": ["percent_change", "turnover_proxy", "volume_proxy"],
            },
            "quote_quality_preferences": {
                "spread_proxy_optional": True,
                "turnover_proxy_optional": True,
                "volume_proxy_optional": True,
                "session_timestamp_required": True,
            },
            "route_readiness": {
                "paper_only": True,
                "live_trade_ready": False,
                "reason": "discovery-first direct local cash quote coverage",
            },
        },
        "why_interesting": "Proxy-led frontier discovery already showed encouraging paper outcomes for South Africa, but current coverage does not confirm whether local cash benchmarks or liquid names are being measured directly and fresh.",
        "inefficiency_hypothesis": "Direct local benchmark or top-liquid cash quotes may preserve frontier signal quality better than ADR, ETF, or offshore proxy mappings.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "benchmark index and top liquid JSE cash names",
        "route_blockers": ["public_endpoint_selection", "index_quote_normalization", "paper_only_local_cash_scope"],
        "recommended_next_action": "adapter_spec",
        "priority": 90,
        "confidence": 0.79,
        "paper_only": True,
    },
    {
        "surface_type_raw": "cash equity benchmark and top-liquid constituent public market data",
        "venue_or_source": "National Stock Exchange of India",
        "country": "India",
        "region": "Asia",
        "asset_or_event": "NIFTY 50, NIFTY Next 50, or top liquid NSE cash equities",
        "data_access_type": "public_no_key",
        "tradability_guess": "watch_only",
        "public_docs_url": "https://www.nseindia.com/market-data/live-equity-market",
        "source_urls": [
            "https://www.nseindia.com/market-data/live-equity-market",
            "https://www.nseindia.com/market-data/live-market-indices",
        ],
        "adapter_request_hint": {
            "paper_only": True,
            "scope_limit": "benchmark_or_top_liquid_names_only",
            "normalized_fields": ["venue", "symbol", "last_price", "timestamp", "freshness_minutes", "route_readiness"],
            "preferred_symbols": ["NIFTY 50", "NIFTY NEXT 50", "RELIANCE", "HDFCBANK", "INFY"],
            "route_readiness": {
                "paper_only": True,
                "live_trade_ready": False,
                "reason": "anti-bot-aware public quote normalization for research only",
            },
        },
        "why_interesting": "India also showed positive early frontier-region paper signals, but the system still lacks bounded direct-local cash discovery for validating freshness and proxy-map quality.",
        "inefficiency_hypothesis": "Direct NSE benchmark or top-constituent quotes may reduce proxy mismatch noise and improve paper discovery ranking in a market with strong local price formation.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "benchmark index and top liquid NSE cash names",
        "route_blockers": ["anti_bot_headers", "index_quote_normalization", "paper_only_local_cash_scope"],
        "recommended_next_action": "adapter_spec",
        "priority": 89,
        "confidence": 0.78,
        "paper_only": True,
    },
    {
        "surface_type_raw": "equity and ETF local exchange market data",
        "venue_or_source": "B3",
        "country": "Brazil",
        "region": "LATAM",
        "asset_or_event": "Brazil equities, ETFs, index futures, and FX futures",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.b3.com.br/en_us/market-data-and-indices/",
        "source_urls": ["https://www.b3.com.br/en_us/market-data-and-indices/"],
        "why_interesting": "Brazil local markets can create ADR, ETF, FX, and futures proxy dislocations.",
        "inefficiency_hypothesis": "Local exchange hours, FX moves, and ADR/ETF proxies may create stale-price edges.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large local exchange",
        "route_blockers": ["broker_route", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 82,
        "confidence": 0.68,
    },
    {
        "surface_type_raw": "futures and options exchange",
        "venue_or_source": "CME Group",
        "country": "United States",
        "region": "North America",
        "asset_or_event": "rates, FX, equity index, metals, energy and agriculture futures/options",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.cmegroup.com/market-data.html",
        "source_urls": ["https://www.cmegroup.com/market-data.html"],
        "why_interesting": "Futures curve, calendar, and cross-asset basis surfaces are globally important.",
        "inefficiency_hypothesis": "Carry, calendar spreads, and ETF/futures proxy gaps can create systematic paper edges.",
        "latency_sensitivity": "high",
        "liquidity_hint": "deep global futures venue",
        "route_blockers": ["futures_account", "market_data_terms"],
        "recommended_next_action": "route_probe",
        "priority": 81,
        "confidence": 0.7,
    },
    {
        "surface_type_raw": "futures and options exchange",
        "venue_or_source": "Eurex",
        "country": "Germany",
        "region": "Europe",
        "asset_or_event": "European equity index, rate, and volatility futures/options",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.eurex.com/ex-en/market-data",
        "source_urls": ["https://www.eurex.com/ex-en/market-data"],
        "why_interesting": "European derivatives can diverge from ETFs, ADRs, and US-session proxies.",
        "inefficiency_hypothesis": "Cross-session stale proxy and futures-basis effects may be measurable.",
        "latency_sensitivity": "high",
        "liquidity_hint": "major derivatives exchange",
        "route_blockers": ["futures_account", "market_data_terms"],
        "recommended_next_action": "route_probe",
        "priority": 78,
        "confidence": 0.66,
    },
    {
        "surface_type_raw": "local exchange equities and derivatives",
        "venue_or_source": "National Stock Exchange of India",
        "country": "India",
        "region": "Asia",
        "asset_or_event": "Indian equities, ETFs, index futures/options",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.nseindia.com/market-data",
        "source_urls": ["https://www.nseindia.com/market-data"],
        "why_interesting": "Large local market with regional hours and global ETF/ADR proxy relationships.",
        "inefficiency_hypothesis": "Market-hours mismatch and proxy products can create delayed reaction opportunities.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large local exchange",
        "route_blockers": ["broker_route", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 78,
        "confidence": 0.64,
    },
    {
        "surface_type_raw": "local exchange equities and derivatives",
        "venue_or_source": "Japan Exchange Group",
        "country": "Japan",
        "region": "Asia",
        "asset_or_event": "Japanese equities, ETFs, futures and options",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.jpx.co.jp/english/markets/",
        "source_urls": ["https://www.jpx.co.jp/english/markets/"],
        "why_interesting": "Japan cash/futures/ETF relationships can be tested against global proxy instruments.",
        "inefficiency_hypothesis": "Overnight global moves and local ETF/futures basis may create short-horizon edges.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large local exchange",
        "route_blockers": ["broker_route", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 77,
        "confidence": 0.63,
    },
    {
        "surface_type_raw": "FX public reference and tradable spot proxies",
        "venue_or_source": "Frankfurter/ECB reference FX",
        "country": "European Union",
        "region": "Global",
        "asset_or_event": "FX reference rates and regional quote normalization",
        "data_access_type": "public_no_key",
        "tradability_guess": "watch_only",
        "public_docs_url": "https://frankfurter.dev/",
        "source_urls": ["https://frankfurter.dev/"],
        "why_interesting": "FX references unlock global regional-market normalization.",
        "inefficiency_hypothesis": "Regional fiat market dislocations need fresh FX references to become comparable.",
        "latency_sensitivity": "low",
        "liquidity_hint": "reference data, not direct executable liquidity",
        "route_blockers": [],
        "recommended_next_action": "hunter_directive",
        "priority": 76,
        "confidence": 0.78,
    },
    {
        "surface_type_raw": "prediction and event market",
        "venue_or_source": "Manifold Markets",
        "country": "United States",
        "region": "Global",
        "asset_or_event": "Public prediction-market probabilities",
        "data_access_type": "public_no_key",
        "tradability_guess": "watch_only",
        "public_docs_url": "https://docs.manifold.markets/api",
        "source_urls": ["https://docs.manifold.markets/api"],
        "why_interesting": "Public probabilities can help event reasoning even when not directly tradable.",
        "inefficiency_hypothesis": "Event probability changes can lead related equities, crypto, FX, or rates proxies.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "event liquidity varies widely",
        "route_blockers": ["tradability_uncertain"],
        "recommended_next_action": "growth_experiment",
        "priority": 75,
        "confidence": 0.62,
    },
    {
        "surface_type_raw": "fixed income public trade tape",
        "venue_or_source": "FINRA TRACE",
        "country": "United States",
        "region": "North America",
        "asset_or_event": "Corporate bond trade reports",
        "data_access_type": "public_no_key",
        "tradability_guess": "watch_only",
        "public_docs_url": "https://www.finra.org/finra-data/browse-catalog/trace",
        "source_urls": ["https://www.finra.org/finra-data/browse-catalog/trace"],
        "why_interesting": "Credit price changes can lead equities, ETFs, options, or CDS proxies.",
        "inefficiency_hypothesis": "Less-followed bond prints may expose credit stress before liquid equity proxies react.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "reported trades, not executable book",
        "route_blockers": ["instrument_eligibility", "broker_route"],
        "recommended_next_action": "growth_experiment",
        "priority": 74,
        "confidence": 0.61,
    },
    {
        "surface_type_raw": "sports and event odds",
        "venue_or_source": "Pinnacle API",
        "country": "Curacao",
        "region": "Global",
        "asset_or_event": "Sports odds and event lines",
        "data_access_type": "broker_account",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.pinnacle.com/en/api",
        "source_urls": ["https://www.pinnacle.com/en/api"],
        "why_interesting": "Event odds can reveal probabilistic information that maps to prediction markets and related assets.",
        "inefficiency_hypothesis": "Cross-market event probability mismatches may expose stale prediction-market or related-asset prices.",
        "latency_sensitivity": "high",
        "liquidity_hint": "account and jurisdiction dependent",
        "route_blockers": ["account_required", "jurisdiction_eligibility"],
        "recommended_next_action": "route_probe",
        "priority": 72,
        "confidence": 0.55,
    },
    {
        "surface_type_raw": "local exchange equities, ETFs, and derivatives",
        "venue_or_source": "London Stock Exchange",
        "country": "United Kingdom",
        "region": "Europe",
        "asset_or_event": "UK equities, ETFs, ADR relationships, and index-linked products",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.londonstockexchange.com/",
        "source_urls": ["https://www.londonstockexchange.com/"],
        "why_interesting": "UK cash equities and ADR/ETF proxies trade across different sessions and currencies.",
        "inefficiency_hypothesis": "Local close, GBP/USD moves, and ADR/ETF proxy repricing can create stale-price or session-gap opportunities.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large developed-market exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 82,
        "confidence": 0.69,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "TMX Group",
        "country": "Canada",
        "region": "North America",
        "asset_or_event": "Canadian equities, ETFs, banks, miners, and CAD-sensitive assets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.tmx.com/",
        "source_urls": ["https://www.tmx.com/"],
        "why_interesting": "Canada has liquid cross-listed equities and commodity-sensitive local markets.",
        "inefficiency_hypothesis": "CAD, commodities, and US-listed proxies may temporarily misprice local Canadian exposure.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large exchange with many cross-listed names",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 80,
        "confidence": 0.68,
    },
    {
        "surface_type_raw": "local exchange equities, ETFs, and derivatives",
        "venue_or_source": "Hong Kong Exchanges and Clearing",
        "country": "Hong Kong",
        "region": "Asia",
        "asset_or_event": "Hong Kong equities, China ADR/H-share relationships, ETFs and derivatives",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.hkex.com.hk/",
        "source_urls": ["https://www.hkex.com.hk/"],
        "why_interesting": "Hong Kong links mainland China, US ADRs, regional ETFs, and local market hours.",
        "inefficiency_hypothesis": "ADR/H-share, ETF, FX, and overnight news relationships can produce short-horizon proxy gaps.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large regional exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 80,
        "confidence": 0.68,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Euronext",
        "country": "Multiple",
        "region": "Europe",
        "asset_or_event": "France, Netherlands, Belgium, Portugal, Ireland and pan-European equity products",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.euronext.com/",
        "source_urls": ["https://www.euronext.com/"],
        "why_interesting": "Pan-European local markets provide many ETF and ADR proxy relationships.",
        "inefficiency_hypothesis": "Regional session timing, EUR moves, and index/sector proxies can produce temporary dislocations.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large multi-country exchange group",
        "route_blockers": ["broker_route", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 79,
        "confidence": 0.66,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Bolsa Mexicana de Valores",
        "country": "Mexico",
        "region": "LATAM",
        "asset_or_event": "Mexican equities, ETFs, ADRs, and MXN-sensitive listed exposure",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.bmv.com.mx/",
        "source_urls": ["https://www.bmv.com.mx/"],
        "why_interesting": "Mexico combines local equities, ADRs, MXN exposure, and LATAM regional flows.",
        "inefficiency_hypothesis": "Local market, ADR, ETF and FX timing gaps may create short-horizon proxy edges.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional exchange with ADR links",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 78,
        "confidence": 0.64,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Taiwan Stock Exchange",
        "country": "Taiwan",
        "region": "Asia",
        "asset_or_event": "Taiwan equities, semiconductors, ETFs and ADR-linked local exposure",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.twse.com.tw/",
        "source_urls": ["https://www.twse.com.tw/"],
        "why_interesting": "Taiwan is semiconductor-heavy and has liquid ETF/ADR proxy exposure.",
        "inefficiency_hypothesis": "Semiconductor news, local hours, and ADR/ETF repricing can create tradable proxy lag.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large regional exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 78,
        "confidence": 0.66,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Korea Exchange",
        "country": "South Korea",
        "region": "Asia",
        "asset_or_event": "Korean equities, index futures/options, ETFs and ADR-linked exposure",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://global.krx.co.kr/",
        "source_urls": ["https://global.krx.co.kr/"],
        "why_interesting": "Korea has liquid global-linked technology, financial and index exposure.",
        "inefficiency_hypothesis": "Local session moves and ADR/ETF proxies may lag each other around regional catalysts.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large regional exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 78,
        "confidence": 0.65,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "SIX Swiss Exchange",
        "country": "Switzerland",
        "region": "Europe",
        "asset_or_event": "Swiss equities, ETFs, defensive mega-cap ADR/local relationships",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.six-group.com/en/products-services/the-swiss-stock-exchange.html",
        "source_urls": ["https://www.six-group.com/en/products-services/the-swiss-stock-exchange.html"],
        "why_interesting": "Swiss defensive equities and CHF exposure can diverge from US-listed proxies.",
        "inefficiency_hypothesis": "CHF moves and local/ADR session timing may create proxy dislocations.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large developed-market exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 77,
        "confidence": 0.63,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Australian Securities Exchange",
        "country": "Australia",
        "region": "Asia-Pacific",
        "asset_or_event": "Australian equities, banks, miners, ETFs and AUD-sensitive assets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.asx.com.au/",
        "source_urls": ["https://www.asx.com.au/"],
        "why_interesting": "Australia links commodities, banks, AUD, local equities, and global miner ADRs.",
        "inefficiency_hypothesis": "Commodity moves, AUD, and local/ADR timing gaps may create proxy lag.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large regional exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 77,
        "confidence": 0.64,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Johannesburg Stock Exchange",
        "country": "South Africa",
        "region": "Africa",
        "asset_or_event": "South African equities, miners, banks, ETFs and ZAR-sensitive assets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.jse.co.za/",
        "source_urls": ["https://www.jse.co.za/"],
        "why_interesting": "South Africa is a frontier/regional market with miners, FX, and ADR-linked proxies.",
        "inefficiency_hypothesis": "ZAR, metals, and local/ADR timing gaps may reveal regional inefficiencies.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional exchange with global-linked resource names",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 76,
        "confidence": 0.62,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Singapore Exchange",
        "country": "Singapore",
        "region": "Asia",
        "asset_or_event": "Singapore equities, REITs, ETFs and regional financial exposure",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.sgx.com/",
        "source_urls": ["https://www.sgx.com/"],
        "why_interesting": "Singapore is a regional hub with cross-Asia financial and REIT exposure.",
        "inefficiency_hypothesis": "Regional flow, SGD, and US-listed proxy timing may create short-horizon gaps.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "regional exchange and derivatives hub",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 76,
        "confidence": 0.6,
    },
    {
        "surface_type_raw": "options and volatility exchange",
        "venue_or_source": "Cboe Global Markets",
        "country": "United States",
        "region": "Global",
        "asset_or_event": "Volatility index futures/options, ETF/ETN proxies, listed options market surfaces",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.cboe.com/",
        "source_urls": ["https://www.cboe.com/"],
        "why_interesting": "Volatility products can move sharply and provide cross-asset risk-regime signals.",
        "inefficiency_hypothesis": "Volatility proxy products may lag risk-on/risk-off moves or term-structure shifts.",
        "latency_sensitivity": "high",
        "liquidity_hint": "large options and volatility venue",
        "route_blockers": ["options_account", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 76,
        "confidence": 0.63,
    },
    {
        "surface_type_raw": "commodities futures and options exchange",
        "venue_or_source": "Intercontinental Exchange",
        "country": "United States",
        "region": "Global",
        "asset_or_event": "Energy, agriculture, rates, FX and equity futures/options surfaces",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.ice.com/",
        "source_urls": ["https://www.ice.com/"],
        "why_interesting": "ICE spans energy, agriculture, rates, FX and global index derivatives.",
        "inefficiency_hypothesis": "Commodity ETF and futures proxy moves may expose short-term carry or news dislocations.",
        "latency_sensitivity": "high",
        "liquidity_hint": "large global derivatives venue",
        "route_blockers": ["futures_account", "market_data_terms"],
        "recommended_next_action": "route_probe",
        "priority": 75,
        "confidence": 0.62,
    },
    {
        "surface_type_raw": "local exchange equities and ETFs",
        "venue_or_source": "Saudi Exchange",
        "country": "Saudi Arabia",
        "region": "Middle East",
        "asset_or_event": "Saudi and GCC equity exposure, ETFs and regional-market proxies",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.saudiexchange.sa/",
        "source_urls": ["https://www.saudiexchange.sa/"],
        "why_interesting": "GCC markets have distinct trading hours, oil sensitivity and regional capital flows.",
        "inefficiency_hypothesis": "Oil moves, local trading week differences, and ETF proxies may create lagged reactions.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "large regional exchange",
        "route_blockers": ["broker_route", "market_data_terms", "fx_normalization"],
        "recommended_next_action": "growth_experiment",
        "priority": 75,
        "confidence": 0.58,
    },
    {
        "surface_type_raw": "metals marketplace and futures reference",
        "venue_or_source": "London Metal Exchange",
        "country": "United Kingdom",
        "region": "Global",
        "asset_or_event": "Industrial metals prices, futures references, warehouse-sensitive markets",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.lme.com/",
        "source_urls": ["https://www.lme.com/"],
        "why_interesting": "Industrial metals can lead miners, FX, inflation expectations, and commodity ETFs.",
        "inefficiency_hypothesis": "Base-metal proxy ETFs and miner equities may lag futures/reference-price shocks.",
        "latency_sensitivity": "medium",
        "liquidity_hint": "global metals venue/reference market",
        "route_blockers": ["futures_account", "market_data_terms"],
        "recommended_next_action": "growth_experiment",
        "priority": 74,
        "confidence": 0.6,
    },
]


def evidence_bundle(source_url: str, claim: str, market_relevance: str, suggested_validation: str) -> dict[str, Any]:
    return {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_url": source_url,
        "claim": claim,
        "market_relevance": market_relevance,
        "suggested_validation": suggested_validation,
        "allowed_use": "research_only_until_sandbox_and_tests_pass",
    }


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _candidate_id(item: dict[str, Any]) -> str:
    key = "|".join(
        str(item.get(field, "")).strip().lower()
        for field in ("surface_type_raw", "venue_or_source", "asset_or_event", "public_docs_url")
    )
    return "gmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _classify_surface(raw: str) -> str:
    text = raw.lower()
    if "crypto" in text:
        if "perp" in text or "future" in text:
            return "crypto_derivatives"
        return "crypto_spot_or_regional"
    if "prediction" in text or "event" in text or "odds" in text:
        return "event_or_prediction"
    if "future" in text or "option" in text or "derivative" in text:
        return "listed_derivatives"
    if "equity" in text or "etf" in text or "adr" in text or "stock" in text:
        return "equity_or_proxy"
    if "fx" in text or "rates" in text:
        return "fx_or_rates"
    if "bond" in text or "credit" in text or "fixed income" in text:
        return "credit_or_fixed_income"
    if "auction" in text or "lending" in text or "marketplace" in text:
        return "alternative_marketplace"
    return "unknown_global_surface"


def _normalize_next_action(item: dict[str, Any]) -> str:
    configured = str(item.get("recommended_next_action") or "").strip()
    if configured in NEXT_ACTIONS:
        return configured
    data_access = str(item.get("data_access_type") or "unknown")
    tradability = str(item.get("tradability_guess") or "unknown")
    if data_access == "public_no_key" and tradability in {"directly_tradable", "route_needed"}:
        return "adapter_spec"
    if data_access in {"broker_account", "public_key_required", "paid_data"} or tradability == "route_needed":
        return "route_probe"
    if int(item.get("priority") or 0) >= 70:
        return "growth_experiment"
    return "watchlist"


def normalize_market_candidate(seed: dict[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
    created = created_at or _utc_now()
    source_urls = seed.get("source_urls") or ([seed["public_docs_url"]] if seed.get("public_docs_url") else [])
    candidate = {
        "candidate_id": "",
        "surface_type_raw": str(seed.get("surface_type_raw") or "unknown"),
        "surface_type_classified": str(seed.get("surface_type_classified") or _classify_surface(str(seed.get("surface_type_raw") or ""))),
        "venue_or_source": str(seed.get("venue_or_source") or "unknown"),
        "country": str(seed.get("country") or "unknown"),
        "region": str(seed.get("region") or "Global"),
        "asset_or_event": str(seed.get("asset_or_event") or "unknown"),
        "data_access_type": str(seed.get("data_access_type") or "unknown"),
        "tradability_guess": str(seed.get("tradability_guess") or "unknown"),
        "public_docs_url": str(seed.get("public_docs_url") or ""),
        "source_urls": [str(url) for url in source_urls if str(url).strip()],
        "why_interesting": str(seed.get("why_interesting") or ""),
        "inefficiency_hypothesis": str(seed.get("inefficiency_hypothesis") or ""),
        "latency_sensitivity": str(seed.get("latency_sensitivity") or "unknown"),
        "liquidity_hint": str(seed.get("liquidity_hint") or "unknown"),
        "route_blockers": [str(item) for item in seed.get("route_blockers", [])],
        "recommended_next_action": str(seed.get("recommended_next_action") or ""),
        "priority": max(1, min(100, int(seed.get("priority") or 50))),
        "confidence": round(max(0.0, min(1.0, float(seed.get("confidence") or 0.5))), 3),
        "created_at": created,
    }
    if candidate["data_access_type"] not in DATA_ACCESS_TYPES:
        candidate["data_access_type"] = "unknown"
    if candidate["tradability_guess"] not in TRADABILITY_GUESSES:
        candidate["tradability_guess"] = "unknown"
    candidate["recommended_next_action"] = _normalize_next_action(candidate)
    candidate["candidate_id"] = _candidate_id(candidate)
    return candidate


def _settings_seeds(settings: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = settings.get("research_worker", {}) if settings else {}
    seeds = cfg.get("discovery_seeds")
    return seeds if isinstance(seeds, list) else []


def discover_market_candidates(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or {}
    cfg = settings.get("research_worker", {})
    if not cfg.get("global_market_discovery", True):
        return []
    seeds = [*DEFAULT_GLOBAL_DISCOVERY_SEEDS, *_settings_seeds(settings)]
    created_at = _utc_now()
    candidates = [normalize_market_candidate(seed, created_at=created_at) for seed in seeds]
    candidates.sort(key=lambda row: (int(row.get("priority") or 0), float(row.get("confidence") or 0)), reverse=True)
    return candidates[: int(cfg.get("max_candidates_per_run", 50))]


def _existing_candidate_ids(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("candidate_id"):
            ids.add(str(item["candidate_id"]))
    return ids


def _append_new_candidates(candidates: Iterable[dict[str, Any]]) -> dict[str, int]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    existing = _existing_candidate_ids(CANDIDATES_JSONL)
    appended = 0
    with CANDIDATES_JSONL.open("a", encoding="utf-8") as handle:
        for candidate in candidates:
            if candidate["candidate_id"] in existing:
                continue
            handle.write(json.dumps(candidate, sort_keys=True) + "\n")
            existing.add(candidate["candidate_id"])
            appended += 1
    return {"total_known": len(existing), "new_appended": appended}


def _source_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "global_market_discovery_worker",
        "candidate_id": candidate["candidate_id"],
        "source_urls": candidate.get("source_urls", []),
        "public_docs_url": candidate.get("public_docs_url"),
        "surface_type_classified": candidate.get("surface_type_classified"),
        "data_access_type": candidate.get("data_access_type"),
        "tradability_guess": candidate.get("tradability_guess"),
        "route_blockers": candidate.get("route_blockers", []),
        "why_interesting": candidate.get("why_interesting"),
        "inefficiency_hypothesis": candidate.get("inefficiency_hypothesis"),
    }


def _implemented_global_discovery_markers(conn: sqlite3.Connection) -> dict[str, set[str]]:
    markers = {
        "market_keys": set(),
        "text": set(),
        "default_venues": {
            str(seed.get("venue_or_source") or "").strip().lower()
            for seed in DEFAULT_GLOBAL_DISCOVERY_SEEDS
            if str(seed.get("venue_or_source") or "").strip()
        },
        "scan_implemented": set(),
    }
    for table in ("adapter_specs", "route_probe_tasks", "market_hunter_directives"):
        try:
            rows = conn.execute(
                f"select market_key from {table} where status = ? and market_key like 'global_discovery|%'",
                (IMPLEMENTED_GLOBAL_DISCOVERY_STATUS,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        markers["market_keys"].update(str(row["market_key"]).lower() for row in rows if row["market_key"])
    try:
        rows = conn.execute(
            """
            select hypothesis
            from growth_experiments
            where status = ?
              and signal_key like 'global_discovery|%'
            """,
            (IMPLEMENTED_GLOBAL_DISCOVERY_STATUS,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    markers["text"].update(str(row["hypothesis"]).lower() for row in rows if row["hypothesis"])
    if markers["market_keys"] or markers["text"]:
        markers["scan_implemented"].add("true")
    return markers


def _global_discovery_candidate_implemented(candidate: dict[str, Any], markers: dict[str, set[str]]) -> bool:
    venue = str(candidate.get("venue_or_source") or "").strip()
    if not venue:
        return False
    market_key = f"global_discovery|{venue}".lower()
    if market_key in markers["market_keys"]:
        return True
    venue_text = venue.lower()
    if markers.get("scan_implemented") and venue_text in markers.get("default_venues", set()):
        return True
    return any(venue_text and venue_text in text for text in markers["text"])


def create_downstream_artifacts(conn: sqlite3.Connection, candidates: list[dict[str, Any]], settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = (settings or {}).get("research_worker", {})
    priority_floor = int(cfg.get("artifact_priority_floor", 70))
    max_artifacts = int(cfg.get("max_artifacts_per_run", 20))
    suppress_implemented = bool(cfg.get("suppress_implemented_global_discovery_artifacts", True))
    implemented_markers = _implemented_global_discovery_markers(conn) if suppress_implemented else {"market_keys": set(), "text": set()}
    created: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(created) >= max_artifacts:
            break
        if int(candidate.get("priority") or 0) < priority_floor:
            continue
        action = candidate.get("recommended_next_action")
        market_key = f"global_discovery|{candidate['venue_or_source']}"
        source_id = f"research:{candidate['candidate_id']}"
        evidence = _source_evidence(candidate)
        created_item: dict[str, Any] | None = None
        if suppress_implemented and _global_discovery_candidate_implemented(candidate, implemented_markers):
            created.append(
                {
                    "type": action,
                    "inserted": False,
                    "skipped": True,
                    "skip_reason": "global_market_discovery_scan_already_implemented",
                    "candidate_id": candidate["candidate_id"],
                    "venue_or_source": candidate["venue_or_source"],
                    "recommended_next_action": action,
                }
            )
            continue
        if action == "adapter_spec":
            inserted = add_adapter_spec(
                conn,
                source_id,
                market_key,
                int(candidate["priority"]),
                f"Global discovery adapter: {candidate['venue_or_source']}",
                {
                    "candidate": candidate,
                    "paper_only": True,
                    "runtime_activation_requires": "sandbox tests and scanner integration",
                },
                evidence,
            )
            created_item = {"type": "adapter_spec", "inserted": inserted}
        elif action == "route_probe":
            inserted = add_route_probe_task(
                conn,
                source_id,
                market_key,
                f"route|{candidate['venue_or_source']}",
                int(candidate["priority"]),
                "global_market_route_feasibility",
                f"Determine tradability and route requirements for {candidate['venue_or_source']}.",
                evidence,
            )
            created_item = {"type": "route_probe_task", "inserted": inserted}
        elif action == "growth_experiment":
            add_growth_experiment(
                conn,
                int(candidate["priority"]),
                f"global_discovery|{candidate['surface_type_classified']}",
                f"Explore {candidate['venue_or_source']} {candidate['asset_or_event']}",
                "Rank public data quality, route feasibility, and paper-testable proxy edges.",
                evidence,
            )
            created_item = {"type": "growth_experiment", "inserted": True}
        elif action in {"hunter_directive", "watchlist"}:
            add_hunter_directive(
                conn,
                market_key,
                "global_market_discovery",
                int(candidate["priority"]),
                candidate.get("why_interesting") or "Global discovery candidate needs exploration slots.",
                evidence,
            )
            created_item = {"type": "market_hunter_directive", "inserted": True}
        if created_item:
            created.append(
                {
                    **created_item,
                    "candidate_id": candidate["candidate_id"],
                    "venue_or_source": candidate["venue_or_source"],
                    "recommended_next_action": action,
                }
            )
    return created


def _summary(candidates: list[dict[str, Any]], artifacts: list[dict[str, Any]], ledger: dict[str, int]) -> dict[str, Any]:
    by_surface: dict[str, int] = {}
    by_region: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_data_access: dict[str, int] = {}
    for candidate in candidates:
        by_surface[candidate["surface_type_classified"]] = by_surface.get(candidate["surface_type_classified"], 0) + 1
        by_region[candidate["region"]] = by_region.get(candidate["region"], 0) + 1
        by_action[candidate["recommended_next_action"]] = by_action.get(candidate["recommended_next_action"], 0) + 1
        by_data_access[candidate["data_access_type"]] = by_data_access.get(candidate["data_access_type"], 0) + 1
    artifact_counts: dict[str, int] = {}
    inserted_counts: dict[str, int] = {}
    for item in artifacts:
        artifact_counts[item["type"]] = artifact_counts.get(item["type"], 0) + 1
        if item.get("inserted"):
            inserted_counts[item["type"]] = inserted_counts.get(item["type"], 0) + 1
    return {
        "candidate_count": len(candidates),
        "new_candidate_count": ledger.get("new_appended", 0),
        "total_known_candidate_count": ledger.get("total_known", 0),
        "by_surface_type": by_surface,
        "by_region": by_region,
        "by_recommended_next_action": by_action,
        "by_data_access_type": by_data_access,
        "artifact_counts": artifact_counts,
        "inserted_artifact_counts": inserted_counts,
        "top_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "venue_or_source": item["venue_or_source"],
                "surface_type_classified": item["surface_type_classified"],
                "region": item["region"],
                "priority": item["priority"],
                "recommended_next_action": item["recommended_next_action"],
            }
            for item in candidates[:10]
        ],
    }


def _report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Research Worker Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Global market discovery: `{report.get('global_market_discovery')}`",
        f"- Web research enabled: `{report.get('web_research_enabled')}`",
        f"- Candidates this run: `{summary.get('candidate_count', 0)}`",
        f"- New candidates appended: `{summary.get('new_candidate_count', 0)}`",
        f"- Total known candidates: `{summary.get('total_known_candidate_count', 0)}`",
        f"- Artifact inserts: `{summary.get('inserted_artifact_counts', {})}`",
        "",
        "## Coverage",
        "",
        f"- Surface types: `{summary.get('by_surface_type', {})}`",
        f"- Regions: `{summary.get('by_region', {})}`",
        f"- Data access: `{summary.get('by_data_access_type', {})}`",
        f"- Next actions: `{summary.get('by_recommended_next_action', {})}`",
        "",
        "## Top Candidates",
        "",
    ]
    for item in summary.get("top_candidates", []):
        lines.append(
            f"- P{item.get('priority')} `{item.get('venue_or_source')}` "
            f"`{item.get('surface_type_classified')}` `{item.get('region')}` "
            f"-> `{item.get('recommended_next_action')}`"
        )
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- Research may cover any public market surface globally.",
            "- Stolen, private, hacked, or non-public information is rejected.",
            "- Live trading, credentials, broker writes, account changes, and real notional changes remain blocked.",
            "- Runtime adapter activation requires sandbox tests and scanner integration.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_once(settings: dict[str, Any] | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    settings = settings or {}
    cfg = settings.get("research_worker", {})
    if not cfg.get("enabled", True):
        report = {
            "generated_at": _utc_now(),
            "status": "disabled",
            "global_market_discovery": False,
            "web_research_enabled": False,
            "candidates": [],
            "created_artifacts": [],
            "summary": {},
        }
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
        return report

    candidates = discover_market_candidates(settings)
    ledger = _append_new_candidates(candidates)
    owns_conn = conn is None
    if owns_conn:
        conn = connect()
    assert conn is not None
    try:
        created_artifacts = create_downstream_artifacts(conn, candidates, settings)
    finally:
        if owns_conn:
            conn.close()

    report = {
        "generated_at": _utc_now(),
        "status": "ok",
        "global_market_discovery": bool(cfg.get("global_market_discovery", True)),
        "web_research_enabled": bool(cfg.get("web_research_enabled", True)),
        "tools": ["global_seed_discovery", "official_docs", "public_market_data_portals", "public_news_or_rss_when_configured"],
        "candidate_ledger": str(CANDIDATES_JSONL),
        "candidates": candidates,
        "created_artifacts": created_artifacts,
        "summary": _summary(candidates, created_artifacts, ledger),
        "hard_rule": "No discovered market activates live trading, credentials, broker writes, account changes, or real notional.",
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run read-only research worker once.")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    if args.config:
        from settings import load_settings

        settings = load_settings(args.config)
    else:
        settings = {}
    report = run_once(settings=settings)
    print(
        f"Research worker status={report['status']} "
        f"candidates={report.get('summary', {}).get('candidate_count', 0)} "
        f"new={report.get('summary', {}).get('new_candidate_count', 0)}"
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
