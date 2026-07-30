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
import pathlib
import sqlite3
import urllib.parse
from typing import Any, Callable, Iterable

from cost_router import ModelResult, complete
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

DISCOVERY_REGIONS = (
    "West Africa",
    "East Africa",
    "Southern Africa",
    "North Africa",
    "Andean Latin America",
    "Southern Cone Latin America",
    "Central America and the Caribbean",
    "Southeast Asia",
    "South Asia",
    "East Asia",
    "Middle East",
    "Eastern Europe and Central Asia",
    "Nordics and Baltics",
    "Oceania and Pacific islands",
)

DISCOVERY_SURFACES = (
    "local securities exchanges with public price or delayed quote access",
    "listed derivatives, options, volatility, futures, and clearing venues",
    "regional fiat, stablecoin, FX, remittance, and cross-border price rails",
    "government bond auctions, rates, credit, and fixed-income price surfaces",
    "commodity auctions, warehouse receipts, agricultural exchanges, and cash markets",
    "power, carbon, renewable certificate, and environmental markets",
    "shipping, freight, insurance, and logistics price benchmarks",
    "sports, prediction, event, and regulated wagering exchanges",
    "public lending, invoice, receivables, and marketplace price feeds",
    "crypto spot, perpetual, options, OTC indication, and local-fiat venues",
    "country ETFs, ADRs, depositary receipts, and cross-listed equity relationships",
    "fund, index, volatility, and alternative-data products with observable prices",
)

PROHIBITED_RESEARCH_TERMS = ("stolen data", "hacked data", "private leak", "inside information")

JSE_DIRECT_DISCOVERY_SEED: dict[str, Any] = {
    "surface_type_raw": "cash equity benchmark and top-liquid constituent public market data",
    "venue_or_source": "Johannesburg Stock Exchange",
    "country": "South Africa",
    "region": "Africa",
    "asset_or_event": "FTSE/JSE Top 40 leaders and liquid local ETFs such as NPN, PRX, SOL, SBK, and STX40",
    "data_access_type": "public_no_key",
    "tradability_guess": "watch_only",
    "public_docs_url": "https://www.jse.co.za/",
    "source_urls": [
        "https://www.jse.co.za/",
        "https://www.jse.co.za/indices",
    ],
    "adapter_route_id": "jse_cash_public_shadow",
    "adapter_request_hint": {
        "method": "GET",
        "path": "/paper/jse/quotes",
        "provider_mode": "public_web_quote_or_delayed_feed",
        "paper_only": True,
        "headers": {
            "Accept": "application/json",
            "User-Agent": "paper-research",
        },
        "response_fields": [
            "symbol",
            "last_price",
            "percent_change",
            "quote_timestamp",
            "spread_bps_proxy",
            "currency",
            "source_venue",
        ],
    },
    "why_interesting": "Direct JSE cash coverage is underrepresented while prior paper evidence for Johannesburg long discovery has been positive.",
    "inefficiency_hypothesis": "Venue-native South African equity and ETF quotes may surface fresher local momentum and liquidity context than broad Yahoo proxy mapping.",
    "latency_sensitivity": "low",
    "liquidity_hint": "benchmark_or_top_liquid_names_only",
    "route_blockers": ["venue_native_symbol_map", "quote_delay_allowed", "spread_proxy_estimation"],
    "recommended_next_action": "adapter_spec",
    "priority": 90,
    "confidence": 0.79,
    "paper_only": True,
    "paper_discovery_only": True,
    "scoring_policy": "paper_only_direct_cash_discovery",
    "quality_gates": {
        "freshness_minutes": 60,
        "maximum_freshness_minutes": 60,
        "spread_bps": "proxy_if_available",
        "max_spread_bps": 150,
        "liquidity_gate": "benchmark_or_top_liquid_names_only",
        "quote_delay_allowed": True,
        "requires_quality_gate_pass": True,
        "shadow_compare_days": 5,
    },
    "venue_quality_metadata": {
        "currency": "ZAR",
        "session_timezone": "Africa/Johannesburg",
        "instrument_scope": "ftse_jse_top_40_and_liquid_local_etfs",
        "price_mode": "venue-native",
    },
}

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
        "venue_or_source": "Saudi Exchange",
        "country": "Saudi Arabia",
        "region": "MENA",
        "asset_or_event": "Tadawul All Share Index (TASI) plus a bounded universe of large/liquid sector leaders such as Saudi Aramco, Al Rajhi Bank, SNB, SABIC, and STC",
        "data_access_type": "public_no_key",
        "tradability_guess": "route_needed",
        "public_docs_url": "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/main-market-watch",
        "source_urls": [
            "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/main-market-watch",
            "https://www.saudiexchange.sa/wps/portal/saudiexchange/ourmarkets/indices-performance",
        ],
        "adapter_route_id": "saudi_exchange_cash_public_shadow",
        "adapter_request_hint": {
            "universe_policy": "bounded_index_and_top_liquid_constituents",
            "max_constituents": 20,
            "freshness_field_hint": "last_trade_time_or_session_timestamp",
            "spread_proxy_hint": "best_bid_ask_if_available_else_high_low_range_proxy",
            "quality_gate_policy": "paper_discovery_only_requires_quote_freshness_and_basic_spread_proxy",
        },
        "why_interesting": "Saudi equities provide distinct oil-linked, domestic retail, and reform-cycle behavior that is not well represented by broad global proxy momentum.",
        "inefficiency_hypothesis": "A bounded TASI-plus-sector-leader universe may surface regional momentum and macro spillover effects without relying on a wide Yahoo-style proxy basket.",
        "latency_sensitivity": "low",
        "liquidity_hint": "main index and top-liquidity constituents only",
        "route_blockers": ["bounded_universe_mapping", "freshness_proxy_validation", "spread_proxy_validation"],
        "recommended_next_action": "adapter_spec",
        "priority": 87,
        "confidence": 0.73,
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
                "baseline_signal_key": "JOHANNESBURG_STOCK_EXCHANGE|global_market_discovery_proxy|long_proxy|standard",
                "shadow_compare_days": 14,
                "comparison_goal": "compare venue-native JSE discovery candidates against current proxy-generated JSE ideas before any broader frontier-equity rollout",
            },
            "quality_gates": {
                "maximum_freshness_minutes": 20,
                "max_spread_bps": 250.0,
                "liquidity_gate": {
                    "require_any": ["turnover_proxy", "volume_proxy"],
                    "min_present_fields": 1,
                },
                "quote_delay_allowed": True,
                "required_fields": ["session_timestamp", "freshness_minutes"],
            },
            "scoring_policy": {
                "paper_only": True,
                "channel": "paper_discovery_only",
                "use_for_live_trades": False,
                "requires_quality_gate_pass": True,
            },
            "expansion_policy": {
                "broader_africa_venue_expansion_blocked": True,
                "requires_quality_gate_pass": True,
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


def _frontier_path() -> pathlib.Path:
    return RUNS_DIR / "research_discovery_frontier.json"


def _journal_path() -> pathlib.Path:
    return RUNS_DIR / "research_discovery_journal.jsonl"


def _theme_id(query: str) -> str:
    return "theme_" + hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:16]


def discovery_theme_catalog() -> list[dict[str, str]]:
    themes: list[dict[str, str]] = []
    for region in DISCOVERY_REGIONS:
        for surface in DISCOVERY_SURFACES:
            query = (
                f"Research {surface} in {region}. Find public, source-backed venues, assets, "
                "instruments, or price feeds that are absent from the current market map."
            )
            themes.append({"theme_id": _theme_id(query), "region": region, "surface": surface, "query": query})
    return themes


def _read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_frontier_state() -> dict[str, Any]:
    payload = _read_json(_frontier_path(), {})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 1)
    payload.setdefault("themes", {})
    payload.setdefault("follow_up_queries", [])
    payload.setdefault("total_search_cycles", 0)
    payload.setdefault("total_new_candidates", 0)
    return payload


def _save_frontier_state(state: dict[str, Any]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _utc_now()
    _frontier_path().write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _parse_time(value: object) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _select_discovery_themes(state: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, str]]:
    limit = max(1, int(cfg.get("search_themes_per_cycle", 2)))
    cooldown = dt.timedelta(hours=max(0.0, float(cfg.get("search_theme_cooldown_hours", 168))))
    now = dt.datetime.now(dt.timezone.utc)
    theme_state = state.setdefault("themes", {})
    pool: list[dict[str, str]] = []

    for follow_up in state.get("follow_up_queries", []):
        if not isinstance(follow_up, dict) or not str(follow_up.get("query") or "").strip():
            continue
        pool.append(
            {
                "theme_id": str(follow_up.get("theme_id") or _theme_id(str(follow_up["query"]))),
                "region": str(follow_up.get("region") or "Global follow-up"),
                "surface": str(follow_up.get("surface") or "model-suggested follow-up"),
                "query": str(follow_up["query"]).strip(),
            }
        )
    pool.extend(discovery_theme_catalog())

    eligible: list[tuple[int, dt.datetime, dict[str, str]]] = []
    cooling: list[tuple[dt.datetime, dict[str, str]]] = []
    earliest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for theme in pool:
        saved = theme_state.get(theme["theme_id"], {})
        last_at = _parse_time(saved.get("last_searched_at")) or earliest
        attempts = int(saved.get("attempts") or 0)
        if last_at == earliest or now - last_at >= cooldown:
            eligible.append((attempts, last_at, theme))
        else:
            cooling.append((last_at, theme))
    eligible.sort(key=lambda item: (item[0], item[1], item[2]["theme_id"]))
    selected = [item[2] for item in eligible[:limit]]
    if len(selected) < limit:
        cooling.sort(key=lambda item: item[0])
        selected.extend(item[1] for item in cooling[: limit - len(selected)])
    return selected


def _candidate_ledger_rows(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    path = path or CANDIDATES_JSONL
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(normalize_market_candidate(payload, created_at=str(payload.get("created_at") or _utc_now())))
    return rows


def _public_source_urls(item: dict[str, Any]) -> list[str]:
    raw_urls = item.get("source_urls") or ([item.get("public_docs_url")] if item.get("public_docs_url") else [])
    urls: list[str] = []
    for raw in raw_urls:
        value = str(raw or "").strip()
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc and value not in urls:
            urls.append(value)
    return urls


def _validate_discovered_item(item: object, cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, "not_an_object"
    if not str(item.get("venue_or_source") or "").strip():
        return None, "missing_venue_or_source"
    if not str(item.get("asset_or_event") or "").strip():
        return None, "missing_asset_or_event"
    searchable_text = json.dumps(item, sort_keys=True).lower()
    if any(term in searchable_text for term in PROHIBITED_RESEARCH_TERMS):
        return None, "non_public_or_prohibited_source"
    urls = _public_source_urls(item)
    if bool(cfg.get("require_public_source_urls", True)) and not urls:
        return None, "missing_public_source_url"
    output = dict(item)
    output["source_urls"] = urls
    output["public_docs_url"] = str(output.get("public_docs_url") or (urls[0] if urls else ""))
    output["source_validation_status"] = "public_url_present" if urls else "source_unverified"
    output["discovered_by"] = "openai_responses_web_search"
    return output, None


def _research_prompt(themes: list[dict[str, str]], known: list[dict[str, Any]], max_results: int) -> str:
    known_venues = sorted({str(item.get("venue_or_source") or "").strip() for item in known if item.get("venue_or_source")})
    known_urls = sorted({url for item in known for url in _public_source_urls(item)})
    schema = {
        "discoveries": [
            {
                "surface_type_raw": "free-form market type",
                "venue_or_source": "official venue/source name",
                "country": "country",
                "region": "region",
                "asset_or_event": "specific new assets, instruments, or events",
                "data_access_type": "public_no_key|public_key_required|broker_account|paid_data|unknown",
                "tradability_guess": "directly_tradable|route_needed|watch_only|unknown",
                "public_docs_url": "https://official-or-authoritative-source",
                "source_urls": ["https://source"],
                "why_interesting": "evidence-based reason",
                "inefficiency_hypothesis": "paper-testable hypothesis",
                "latency_sensitivity": "low|medium|high|unknown",
                "liquidity_hint": "what the source indicates",
                "route_blockers": ["unknown or documented blockers"],
                "priority": "integer 1-100 opportunity priority; this is not a 1-5 rank",
                "confidence": 0.0,
            }
        ],
        "follow_up_queries": ["new research direction grounded in a source found this run"],
        "search_notes": "brief description of searches performed",
    }
    return (
        "Use web search to discover genuinely new public market surfaces. Do not repeat the known catalog, "
        "do not merely rewrite the assigned themes, and do not rely on memory without a source. Search official "
        "venue/exchange/API pages first; authoritative public registries or notices are acceptable when official "
        "API docs do not exist. Unknown market types are welcome. Every discovery must name specific assets or "
        "instruments and include at least one public HTTP source URL. Return no more than "
        f"{max_results} discoveries as exactly one JSON object matching this shape:\n{json.dumps(schema, sort_keys=True)}\n\n"
        f"Research themes for this cycle:\n{json.dumps(themes, sort_keys=True)}\n\n"
        f"Known venues/sources to avoid repeating:\n{json.dumps(known_venues[-250:], sort_keys=True)}\n\n"
        f"Known source URLs to avoid repeating:\n{json.dumps(known_urls[-250:], sort_keys=True)}"
    )


def _append_research_journal(entry: dict[str, Any]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with _journal_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _decode_research_json(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    elif value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(value):
            if char != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(value[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    return parsed if isinstance(parsed, dict) else None


def run_continuous_web_discovery(
    settings: dict[str, Any],
    known_candidates: list[dict[str, Any]],
    *,
    model_complete: Callable[..., ModelResult] = complete,
) -> dict[str, Any]:
    cfg = settings.get("research_worker", {})
    if not bool(cfg.get("web_research_enabled", False)):
        return {"status": "disabled", "candidates": [], "selected_themes": [], "rejected": []}

    state = _load_frontier_state()
    themes = _select_discovery_themes(state, cfg)
    max_results = max(1, int(cfg.get("max_web_discoveries_per_cycle", 8)))
    prompt = _research_prompt(themes, known_candidates, max_results)
    result = model_complete(
        "global_research_worker",
        prompt,
        system=(
            "You are a global market-discovery researcher. Search broadly across countries and asset classes. "
            "Evidence and novelty matter more than familiarity. Return exactly one JSON object."
        ),
        tier_override=str(cfg.get("model_tier") or "fast"),
        operation="global_market_discovery_web_search",
        reasoning_effort_override=str(cfg.get("reasoning_effort") or "low"),
        structured_json=False,
        max_output_tokens_override=int(cfg.get("max_output_tokens", 5000)),
        timeout_seconds_override=float(cfg.get("timeout_seconds", 120)),
        tools=[{"type": "web_search"}],
    )
    now = _utc_now()
    status = "ok" if str(result.status).startswith("model_call:") else str(result.status)
    payload: dict[str, Any] = {}
    if status == "ok":
        decoded = _decode_research_json(result.text)
        if decoded is None:
            status = "invalid_model_json"
        else:
            payload = decoded

    known_ids = {str(item.get("candidate_id") or _candidate_id(item)) for item in known_candidates}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if status == "ok":
        for raw in payload.get("discoveries", [])[:max_results]:
            validated, reason = _validate_discovered_item(raw, cfg)
            if validated is None:
                rejected.append({"reason": reason, "item": raw})
                continue
            validated["research_query_ids"] = [theme["theme_id"] for theme in themes]
            candidate = normalize_market_candidate(validated, created_at=now)
            if candidate["candidate_id"] in known_ids:
                rejected.append({"reason": "known_candidate", "candidate_id": candidate["candidate_id"]})
                continue
            accepted.append(candidate)
            known_ids.add(candidate["candidate_id"])

    theme_state = state.setdefault("themes", {})
    for theme in themes:
        saved = theme_state.setdefault(theme["theme_id"], {})
        saved.update(
            {
                "query": theme["query"],
                "region": theme["region"],
                "surface": theme["surface"],
                "last_searched_at": now,
                "attempts": int(saved.get("attempts") or 0) + 1,
                "last_status": status,
                "last_new_candidates": len(accepted),
            }
        )
    existing_followups = {
        str(item.get("theme_id") or ""): item
        for item in state.get("follow_up_queries", [])
        if isinstance(item, dict)
    }
    for query in payload.get("follow_up_queries", [])[:12] if isinstance(payload, dict) else []:
        query_text = str(query or "").strip()
        if not query_text:
            continue
        followup_id = _theme_id(query_text)
        existing_followups.setdefault(
            followup_id,
            {"theme_id": followup_id, "query": query_text, "created_at": now, "region": "Global follow-up"},
        )
    state["follow_up_queries"] = list(existing_followups.values())[-250:]
    state["total_search_cycles"] = int(state.get("total_search_cycles") or 0) + 1
    state["total_new_candidates"] = int(state.get("total_new_candidates") or 0) + len(accepted)
    _save_frontier_state(state)

    journal = {
        "searched_at": now,
        "status": status,
        "themes": themes,
        "new_candidate_ids": [item["candidate_id"] for item in accepted],
        "rejected_counts": {
            reason: sum(1 for item in rejected if item.get("reason") == reason)
            for reason in sorted({str(item.get("reason")) for item in rejected})
        },
        "follow_up_query_count": len(payload.get("follow_up_queries", [])) if isinstance(payload, dict) else 0,
        "model": result.model_name,
        "model_tier": result.model_tier,
        "model_status": result.status,
        "estimated_cost_usd": result.estimated_cost_usd,
    }
    _append_research_journal(journal)
    return {
        "status": status,
        "candidates": accepted,
        "selected_themes": themes,
        "rejected": rejected,
        "follow_up_queries": payload.get("follow_up_queries", []) if isinstance(payload, dict) else [],
        "model": result.model_name,
        "model_tier": result.model_tier,
        "model_status": result.status,
        "estimated_cost_usd": result.estimated_cost_usd,
        "journal": str(_journal_path()),
        "frontier": str(_frontier_path()),
    }


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


def _normalize_priority(value: object) -> int:
    try:
        priority = float(value or 50)
    except (TypeError, ValueError):
        priority = 50.0
    if 0.0 < priority <= 5.0:
        priority = 50.0 + (priority * 8.0)
    return max(1, min(100, int(round(priority))))


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
    return _normalize_next_actions(item)[0]


def _normalize_next_actions(item: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    configured_many = item.get("recommended_next_actions")
    if isinstance(configured_many, list):
        actions.extend(str(action).strip() for action in configured_many if str(action).strip() in NEXT_ACTIONS)
    configured = str(item.get("recommended_next_action") or "").strip()
    if configured in NEXT_ACTIONS and configured not in actions:
        actions.append(configured)
    data_access = str(item.get("data_access_type") or "unknown")
    tradability = str(item.get("tradability_guess") or "unknown")
    route_blockers = item.get("route_blockers") or []
    if data_access in {"public_no_key", "public_key_required"} and "adapter_spec" not in actions:
        actions.append("adapter_spec")
    if (
        data_access in {"broker_account", "public_key_required", "paid_data"}
        or tradability == "route_needed"
        or bool(route_blockers)
    ) and "route_probe" not in actions:
        actions.append("route_probe")
    if int(item.get("priority") or 0) >= 70 and not actions:
        actions.append("growth_experiment")
    if not actions:
        actions.append("watchlist")
    return actions


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
        "recommended_next_actions": list(seed.get("recommended_next_actions") or []),
        "priority": _normalize_priority(seed.get("priority")),
        "confidence": round(max(0.0, min(1.0, float(seed.get("confidence") or 0.5))), 3),
        "source_validation_status": str(seed.get("source_validation_status") or "seed_or_configured_source"),
        "discovered_by": str(seed.get("discovered_by") or "configured_seed"),
        "research_query_ids": [str(item) for item in seed.get("research_query_ids", [])],
        "created_at": created,
    }
    if candidate["data_access_type"] not in DATA_ACCESS_TYPES:
        candidate["data_access_type"] = "unknown"
    if candidate["tradability_guess"] not in TRADABILITY_GUESSES:
        candidate["tradability_guess"] = "unknown"
    candidate["recommended_next_actions"] = _normalize_next_actions(candidate)
    candidate["recommended_next_action"] = candidate["recommended_next_actions"][0]
    candidate["candidate_id"] = _candidate_id(candidate)
    return candidate


def _settings_seeds(settings: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = settings.get("research_worker", {}) if settings else {}
    seeds = cfg.get("discovery_seeds")
    return seeds if isinstance(seeds, list) else []


def discover_market_candidates(
    settings: dict[str, Any] | None = None,
    *,
    discovered_candidates: list[dict[str, Any]] | None = None,
    include_bootstrap: bool = True,
) -> list[dict[str, Any]]:
    settings = settings or {}
    cfg = settings.get("research_worker", {})
    if not cfg.get("global_market_discovery", True):
        return []
    seeds = [*DEFAULT_GLOBAL_DISCOVERY_SEEDS, *_settings_seeds(settings)] if include_bootstrap else []
    seeds.extend(discovered_candidates or [])
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


def _append_new_candidates(candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    existing = _existing_candidate_ids(CANDIDATES_JSONL)
    appended = 0
    new_candidate_ids: list[str] = []
    with CANDIDATES_JSONL.open("a", encoding="utf-8") as handle:
        for candidate in candidates:
            if candidate["candidate_id"] in existing:
                continue
            handle.write(json.dumps(candidate, sort_keys=True) + "\n")
            existing.add(candidate["candidate_id"])
            appended += 1
            new_candidate_ids.append(str(candidate["candidate_id"]))
    return {"total_known": len(existing), "new_appended": appended, "new_candidate_ids": new_candidate_ids}


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


def _artifact_exists(conn: sqlite3.Connection, action: str, source_id: str, candidate: dict[str, Any]) -> bool:
    if action == "adapter_spec":
        return conn.execute(
            "select 1 from adapter_specs where source_recommendation_id = ? limit 1",
            (source_id,),
        ).fetchone() is not None
    if action == "route_probe":
        return conn.execute(
            """
            select 1 from route_probe_tasks
            where source_recommendation_id = ? and probe_type = 'global_market_route_feasibility'
            limit 1
            """,
            (source_id,),
        ).fetchone() is not None
    if action == "growth_experiment":
        hypothesis = f"Explore {candidate['venue_or_source']} {candidate['asset_or_event']}"
        return conn.execute(
            "select 1 from growth_experiments where hypothesis = ? limit 1",
            (hypothesis,),
        ).fetchone() is not None
    if action in {"hunter_directive", "watchlist"}:
        return conn.execute(
            """
            select 1 from market_hunter_directives
            where market_key = ? and directive = 'global_market_discovery'
            limit 1
            """,
            (f"global_discovery|{candidate['venue_or_source']}",),
        ).fetchone() is not None
    return False


def create_downstream_artifacts(conn: sqlite3.Connection, candidates: list[dict[str, Any]], settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = (settings or {}).get("research_worker", {})
    priority_floor = int(cfg.get("artifact_priority_floor", 70))
    max_artifacts = int(cfg.get("max_artifacts_per_run", 20))
    created: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(created) >= max_artifacts:
            break
        if int(candidate.get("priority") or 0) < priority_floor:
            continue
        market_key = f"global_discovery|{candidate['venue_or_source']}"
        source_id = f"research:{candidate['candidate_id']}"
        evidence = _source_evidence(candidate)
        actions = _normalize_next_actions(candidate)
        for action in actions:
            if len(created) >= max_artifacts:
                break
            if _artifact_exists(conn, action, source_id, candidate):
                continue
            created_item: dict[str, Any] | None = None
            if action == "adapter_spec":
                inserted = add_adapter_spec(
                    conn,
                    source_id,
                    market_key,
                    int(candidate["priority"]),
                    f"Global discovery adapter: {candidate['venue_or_source']} - {candidate['asset_or_event']}",
                    {
                        "candidate": candidate,
                        "paper_only": True,
                        "lifecycle": "data_adapter_only",
                        "runtime_activation_requires": "sandbox tests, scanner integration, and observed data health",
                    },
                    evidence,
                )
                created_item = {"type": "adapter_spec", "inserted": inserted}
            elif action == "route_probe":
                route_evidence = {
                    **evidence,
                    "lifecycle": "route_verification_independent_of_adapter",
                    "resolution_rule": "Remain open until route requirements are confirmed, rejected, or explicitly waived.",
                }
                inserted = add_route_probe_task(
                    conn,
                    source_id,
                    market_key,
                    f"route|{candidate['venue_or_source']}|{candidate['asset_or_event']}",
                    int(candidate["priority"]),
                    "global_market_route_feasibility",
                    f"Verify tradability and route requirements for {candidate['venue_or_source']} {candidate['asset_or_event']}.",
                    route_evidence,
                )
                created_item = {"type": "route_probe_task", "inserted": inserted}
            elif action == "growth_experiment":
                before = conn.total_changes
                add_growth_experiment(
                    conn,
                    int(candidate["priority"]),
                    f"global_discovery|{candidate['surface_type_classified']}",
                    f"Explore {candidate['venue_or_source']} {candidate['asset_or_event']}",
                    "Rank public data quality, route feasibility, and paper-testable edges without treating discovery as route approval.",
                    evidence,
                )
                created_item = {"type": "growth_experiment", "inserted": conn.total_changes > before}
            elif action in {"hunter_directive", "watchlist"}:
                before = conn.total_changes
                add_hunter_directive(
                    conn,
                    market_key,
                    "global_market_discovery",
                    int(candidate["priority"]),
                    candidate.get("why_interesting") or "Global discovery candidate needs exploration slots.",
                    evidence,
                )
                created_item = {"type": "market_hunter_directive", "inserted": conn.total_changes > before}
            if created_item and created_item.get("inserted"):
                created.append(
                    {
                        **created_item,
                        "candidate_id": candidate["candidate_id"],
                        "venue_or_source": candidate["venue_or_source"],
                        "recommended_next_action": action,
                        "recommended_next_actions": actions,
                    }
                )
    return created


def reconcile_discovery_route_lifecycle(conn: sqlite3.Connection) -> dict[str, int]:
    """Undo legacy route completion caused only by discovering a market surface."""

    reopened = conn.execute(
        """
        update route_probe_tasks
        set status = 'open'
        where status = ?
          and (source_recommendation_id like 'research:%' or market_key like 'global_discovery|%')
        """,
        (IMPLEMENTED_GLOBAL_DISCOVERY_STATUS,),
    ).rowcount
    conn.commit()
    open_count = int(
        conn.execute(
            "select count(*) from route_probe_tasks where status = 'open' and market_key like 'global_discovery|%'"
        ).fetchone()[0]
    )
    return {"reopened_legacy_route_probes": int(reopened), "open_global_route_probes": open_count}


def _summary(
    candidates: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    ledger: dict[str, Any],
    web_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_surface: dict[str, int] = {}
    by_region: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_data_access: dict[str, int] = {}
    for candidate in candidates:
        by_surface[candidate["surface_type_classified"]] = by_surface.get(candidate["surface_type_classified"], 0) + 1
        by_region[candidate["region"]] = by_region.get(candidate["region"], 0) + 1
        for action in candidate.get("recommended_next_actions", [candidate["recommended_next_action"]]):
            by_action[action] = by_action.get(action, 0) + 1
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
        "discovery_status": (web_discovery or {}).get("status", "not_run"),
        "search_themes_run": len((web_discovery or {}).get("selected_themes", [])),
        "rejected_discovery_count": len((web_discovery or {}).get("rejected", [])),
        "novelty_rate": round(
            ledger.get("new_appended", 0)
            / max(1, ledger.get("new_appended", 0) + len((web_discovery or {}).get("rejected", []))),
            4,
        ),
        "top_candidates": [
            {
                "candidate_id": item["candidate_id"],
                "venue_or_source": item["venue_or_source"],
                "surface_type_classified": item["surface_type_classified"],
                "region": item["region"],
                "priority": item["priority"],
                "recommended_next_action": item["recommended_next_action"],
                "recommended_next_actions": item.get("recommended_next_actions", [item["recommended_next_action"]]),
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
        f"- Continuous discovery status: `{summary.get('discovery_status', 'not_run')}`",
        f"- Search themes run: `{summary.get('search_themes_run', 0)}`",
        f"- Source-backed novelty rate: `{summary.get('novelty_rate', 0.0)}`",
        f"- Artifact inserts: `{summary.get('inserted_artifact_counts', {})}`",
        f"- Route lifecycle: `{report.get('route_lifecycle', {})}`",
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
            f"-> `{item.get('recommended_next_actions', [item.get('recommended_next_action')])}`"
        )
    selected_themes = (report.get("continuous_discovery") or {}).get("selected_themes", [])
    lines.extend(["", "## Research Frontier", ""])
    if selected_themes:
        for theme in selected_themes:
            lines.append(f"- `{theme.get('region')}` / `{theme.get('surface')}`")
    else:
        lines.append("- No web-search theme completed this cycle; see discovery status above.")
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


def run_once(
    settings: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    model_complete: Callable[..., ModelResult] = complete,
) -> dict[str, Any]:
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

    known_candidates = _candidate_ledger_rows()
    web_discovery = run_continuous_web_discovery(
        settings,
        known_candidates,
        model_complete=model_complete,
    )
    include_bootstrap = bool(cfg.get("bootstrap_seed_catalog_once", True)) and not known_candidates
    candidates = discover_market_candidates(
        settings,
        discovered_candidates=web_discovery.get("candidates", []),
        include_bootstrap=include_bootstrap,
    )
    ledger = _append_new_candidates(candidates)
    artifact_pool = {
        str(candidate.get("candidate_id")): candidate
        for candidate in [*known_candidates, *candidates]
        if candidate.get("candidate_id")
    }
    artifact_candidates = sorted(
        artifact_pool.values(),
        key=lambda row: (int(row.get("priority") or 0), float(row.get("confidence") or 0.0)),
        reverse=True,
    )
    owns_conn = conn is None
    if owns_conn:
        conn = connect()
    assert conn is not None
    try:
        route_lifecycle = reconcile_discovery_route_lifecycle(conn)
        created_artifacts = create_downstream_artifacts(conn, artifact_candidates, settings)
        route_lifecycle["open_global_route_probes"] = int(
            conn.execute(
                "select count(*) from route_probe_tasks where status = 'open' and market_key like 'global_discovery|%'"
            ).fetchone()[0]
        )
    finally:
        if owns_conn:
            conn.close()

    report = {
        "generated_at": _utc_now(),
        "status": "ok",
        "global_market_discovery": bool(cfg.get("global_market_discovery", True)),
        "web_research_enabled": bool(cfg.get("web_research_enabled", False)),
        "tools": ["openai_responses_web_search", "official_docs", "public_market_data_portals", "public_news_or_rss"],
        "candidate_ledger": str(CANDIDATES_JSONL),
        "discovery_frontier": str(_frontier_path()),
        "discovery_journal": str(_journal_path()),
        "continuous_discovery": web_discovery,
        "route_lifecycle": route_lifecycle,
        "candidates": candidates,
        "created_artifacts": created_artifacts,
        "summary": _summary(candidates, created_artifacts, ledger, web_discovery),
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
