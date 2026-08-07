from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from paper_exploration import prepare_candidate_for_exploration  # noqa: E402
from radar_loop import _select_runtime_strategy_lab_candidates  # noqa: E402
from storage import init_db, open_paper_trade, record_due_horizon_outcomes  # noqa: E402
from strategy_lab import (  # noqa: E402
    _observation_program_inputs,
    _paper_route_eligible_candidates,
    _queue_promotion,
    _runtime_contract_program,
    _runtime_entry_invalidation_contract_mismatch,
    _runtime_universe_contract_mismatch,
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
)
from strategy_program import (  # noqa: E402
    ProgramValidationError,
    _load_history,
    assert_plugin_parity,
    compile_observation_program,
    evaluate_expression,
    generate_program_candidates,
    novelty_signature,
    record_feature_snapshots,
)


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings() -> dict:
    output = copy.deepcopy(DEFAULT_SETTINGS)
    output["allow_live_trading"] = False
    output["strategy_lab"]["feature_snapshot_max_rows"] = 2_000_000
    return output


def program_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {"venues": ["YAHOO_PROXY"], "asset_classes": ["equity"]},
        "calculated_features": {
            "cost_adjusted_momentum": "return_5m_bps - spread_bps",
        },
        "entry_expression": "quality_score >= 60 and cost_adjusted_momentum > 5",
        "invalidation_expression": "stale_minutes > 5",
        "long_expression": "cost_adjusted_momentum > 0",
        "short_expression": "cost_adjusted_momentum < -20",
        "edge_expression": "max(cost_adjusted_momentum, 0)",
        "score_expression": "clip(50 + cost_adjusted_momentum / 2, 0, 100)",
        "route_surface": "proxy",
    }


def shock_reversal_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {"venues": ["YAHOO_PROXY"]},
        "calculated_features": {
            "shock_magnitude_bps": "abs(return_60m_bps)",
            "shock_sigma": "abs(return_60m_bps) / max(volatility_60m_bps, 10)",
            "flip_strength_bps": "max(0, -(return_5m_bps * return_60m_bps) / max(abs(return_60m_bps), 1))",
            "cost_adjusted_reversal_edge_bps": "max(0, min(0.15 * abs(return_60m_bps) + flip_strength_bps, 40) - 2 * spread_bps)",
        },
        "entry_expression": (
            "shock_magnitude_bps >= 40 and shock_sigma >= 1.75 "
            "and return_5m_bps * return_60m_bps < 0 and flip_strength_bps >= 5 "
            "and spread_bps <= 8 and liquidity_score >= 0.65 "
            "and quality_score >= 60 and stale_minutes <= 5"
        ),
        "invalidation_expression": (
            "shock_magnitude_bps < 25 or return_5m_bps * return_60m_bps >= 0 "
            "or spread_bps > 12 or quality_score < 55 or stale_minutes > 10"
        ),
        "long_expression": "return_60m_bps < 0 and return_5m_bps > 0",
        "short_expression": "return_60m_bps > 0 and return_5m_bps < 0",
        "edge_expression": "cost_adjusted_reversal_edge_bps",
        "score_expression": "clip(30 + 12 * min(shock_sigma, 4) + min(flip_strength_bps, 20), 0, 100)",
        "route_surface": "proxy",
        "output_trade_type": "global_proxy_shock_reversal",
    }


def funding_capture_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["OKX"],
            "trade_types": ["perp_funding_basis"],
            "quotes": ["USDT"],
        },
        "calculated_features": {
            "predicted_next_funding_bps": (
                "min(funding_bps, funding_history_last_bps, funding_history_avg_bps)"
            ),
            "basis_instability_bps": (
                "basis_volatility_60m_bps + abs(basis_change_5m_bps)"
            ),
            "cost_adjusted_carry_edge_bps": (
                "max(0, net_carry_edge_bps - 0.5 * basis_volatility_60m_bps "
                "- abs(basis_change_5m_bps))"
            ),
        },
        "entry_expression": (
            "basis_history_ready >= 1 and funding_history_count >= 3 "
            "and predicted_next_funding_bps > 0 and net_carry_edge_bps > 3 "
            "and abs(basis_zscore_60m) <= 1 and basis_volatility_60m_bps <= 6 "
            "and abs(basis_change_5m_bps) <= 3 and spread_bps <= 4 "
            "and liquidity_score >= 0.7 and quality_score >= 65 and stale_minutes <= 2"
        ),
        "invalidation_expression": (
            "predicted_next_funding_bps <= 0 or abs(basis_zscore_60m) > 1.5 "
            "or basis_volatility_60m_bps > 10 or abs(basis_change_5m_bps) > 6 "
            "or spread_bps > 8 or stale_minutes > 5"
        ),
        "direction": "short",
        "edge_expression": "cost_adjusted_carry_edge_bps",
        "score_expression": (
            "clip(50 + 3 * cost_adjusted_carry_edge_bps "
            "- 8 * abs(basis_zscore_60m) - basis_instability_bps - spread_bps, 0, 100)"
        ),
        "route_surface": "perp",
        "output_trade_type": "perp_funding_capture",
    }


def funding_observation(price: float, basis_bps: float, observed_at: str) -> dict:
    return {
        "inst_id": "BTC-USDT-SWAP",
        "venue": "OKX",
        "trade_type": "perp_funding_basis",
        "asset_class": "crypto_linked_derivative",
        "quote": "USDT",
        "base": "BTC",
        "last": price,
        "basis_bps": basis_bps,
        "funding_bps": 8.0,
        "funding_history_count": 8,
        "funding_history_avg_bps": 6.0,
        "funding_history_last_bps": 7.0,
        "net_carry_edge_bps": 12.0,
        "round_trip_cost_bps": 4.0,
        "spread_bps": 2.0,
        "liquidity_score": 0.85,
        "quality_score": 90.0,
        "quality_status": "verified",
        "stale_minutes": 1.0,
        "observed_at": observed_at,
        "price_source": "fixture",
    }


def lab_recommendation(strategy_lab_id: str = "observation_momentum_v1", logic: dict | None = None) -> dict:
    return {
        "recommendation_id": "rec_" + strategy_lab_id,
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "title": "Test observation-native cost-adjusted momentum",
            "rationale": "Test a reusable price-history hypothesis without depending on scanner candidates.",
            "strategy_lab_experiment": {
                "strategy_lab_id": strategy_lab_id,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": "Liquid instruments with fresh quality-confirmed momentum continue after costs.",
                "source_surface": "proxy",
                "permitted_target_surface": ["proxy"],
                "strategy_logic": logic or program_logic(),
                "data_requirements": {"paper_only": True},
                "risk_gates": {},
                "promotion_rules": {},
            },
        },
    }


def observation(price: float, observed_at: str) -> dict:
    return {
        "inst_id": "TEST:ABC",
        "venue": "YAHOO_PROXY",
        "trade_type": "global_market_discovery_proxy",
        "market_type": "equity",
        "asset_class": "equity",
        "region": "global",
        "last": price,
        "spread_bps": 2.0,
        "liquidity_score": 0.8,
        "quality_score": 80.0,
        "quality_status": "verified",
        "stale_minutes": 0.0,
        "observed_at": observed_at,
        "price_source": "fixture",
    }


def adx_nav_observation(price: float, observed_at: str, **overrides: object) -> dict:
    row = {
        "inst_id": "ADX:ETF:CHADX15",
        "venue": "ADX",
        "trade_type": "official_factsheet_nav_reference",
        "market_type": "exchange_traded_fund",
        "asset_class": "equity_etf",
        "quote": "AED",
        "base": "CHADX15",
        "last": price,
        "spread_bps": 0.0,
        "liquidity_score": 0.5,
        "quality_score": 75.0,
        "quality_status": "official_month_end_nav",
        "market_surface": "adx_official_etf_factsheet_nav",
        "freshness_state": "fresh",
        "candidate_reject_reason": "factsheet_nav_not_entry_quality_quote",
        "stale_minutes": 1.0,
        "session_status": "open",
        "observed_at": observed_at,
        "price_source": "ADX official ETF factsheet NAV",
    }
    row.update(overrides)
    return row


def adx_nav_reference_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["ADX"],
            "asset_classes": ["equity_etf"],
            "market_types": ["exchange_traded_fund"],
        },
        "calculated_features": {"nav_update_edge_bps": "return_5m_bps - 5"},
        "entry_expression": (
            "market_surface == 'adx_official_etf_factsheet_nav' "
            "and quality_status == 'official_month_end_nav' "
            "and candidate_reject_reason == 'factsheet_nav_not_entry_quality_quote' "
            "and freshness_state == 'fresh' and return_5m_bps >= 50"
        ),
        "invalidation_expression": "freshness_state != 'fresh'",
        "direction": "long",
        "edge_expression": "nav_update_edge_bps",
        "score_expression": "clip(50 + nav_update_edge_bps / 4, 0, 100)",
        "route_surface": "nav_reference",
    }


def adx_derivatives_companion_observation(
    price: float,
    observed_at: str,
    *,
    inst_id: str = "ADX:FUTURES:SSF_ADNOC_GAS",
    base: str = "ADNOC_GAS",
    symbol: str = "SSF_ADNOC_GAS",
    **overrides: object,
) -> dict:
    row = {
        "inst_id": inst_id,
        "venue": "ADX",
        "trade_type": "official_derivatives_contract_reference",
        "market_type": "futures",
        "asset_class": "single_stock_futures",
        "quote": "AED",
        "base": base,
        "symbol": symbol,
        "last": price,
        "spread_bps": 3.0,
        "liquidity_score": 0.7,
        "quality_score": 80.0,
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "price_available": True,
        "price_basis": "public_companion_underlying_spot_quote",
        "market_surface": "adx_equity_and_index_futures_contract_catalog",
        "freshness_state": "fresh",
        "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
        "stale_minutes": 1.0,
        "session_status": "unknown",
        "observed_at": observed_at,
        "price_source": "TradingView public ADX companion quote",
        "source_url": "https://www.tradingview.com/symbols/ADX-ADNOCGAS/",
        "source_contract_url": "https://www.adx.ae/about-adx/media/adx-news/adx-lists-six-new-single-stock-futures-bloomberg-collaboration",
        "companion_quote_symbol": "ADNOCGAS",
        "proxy_symbol": "ADX:ADNOCGAS",
    }
    row.update(overrides)
    return row


def adx_derivatives_companion_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["ADX"],
            "trade_types": ["official_derivatives_contract_reference"],
            "market_surfaces": ["adx_equity_and_index_futures_contract_catalog"],
        },
        "calculated_features": {
            "companion_return_strength_bps": "abs(return_5m_bps)",
        },
        "entry_expression": (
            "market_surface == 'adx_equity_and_index_futures_contract_catalog' "
            "and price_basis == 'public_companion_underlying_spot_quote' "
            "and quality_status == 'verified_proxy' and freshness_state == 'fresh' "
            "and last > 0"
        ),
        "invalidation_expression": "freshness_state != 'fresh' or last <= 0",
        "long_expression": "return_5m_bps > 0",
        "short_expression": "return_5m_bps < 0",
        "edge_expression": "companion_return_strength_bps",
        "score_expression": "clip(45 + companion_return_strength_bps / 4, 0, 100)",
        "route_surface": "proxy",
    }


def b3_bdr_etf_companion_observation(
    price: float,
    observed_at: str,
    *,
    inst_id: str = "B3:PUBLIC_DATA_SURFACE:BDR_ETF",
    symbol: str = "BDR_ETF",
    **overrides: object,
) -> dict:
    row = {
        "inst_id": inst_id,
        "venue": "B3",
        "trade_type": "official_market_catalog",
        "market_type": "cash_equity_reference",
        "asset_class": "bdr_etf",
        "quote": "USD",
        "base": "BDR_ETF",
        "symbol": symbol,
        "last": price,
        "spread_bps": 3.0,
        "liquidity_score": 0.7,
        "quality_score": 80.0,
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "price_available": True,
        "price_basis": "public_companion_brazil_equity_etf_quote",
        "market_surface": "b3_bdr_etf_public_data",
        "freshness_state": "fresh",
        "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
        "stale_minutes": 1.0,
        "session_status": "unknown",
        "observed_at": observed_at,
        "price_source": "TradingView public Brazil ETF companion quote",
        "source_url": "https://www.tradingview.com/symbols/AMEX-EWZ/",
        "source_contract_url": "https://www.b3.com.br/pt_br/bdr-etf.htm",
        "companion_quote_symbol": "EWZ",
        "proxy_symbol": "AMEX:EWZ",
    }
    row.update(overrides)
    return row


def b3_bdr_etf_companion_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["B3"],
            "trade_types": ["official_market_catalog"],
            "market_surfaces": ["b3_bdr_etf_public_data"],
        },
        "calculated_features": {
            "companion_return_strength_bps": "abs(return_5m_bps)",
        },
        "entry_expression": (
            "market_surface == 'b3_bdr_etf_public_data' "
            "and price_basis == 'public_companion_brazil_equity_etf_quote' "
            "and quality_status == 'verified_proxy' and freshness_state == 'fresh' "
            "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
            "and last > 0"
        ),
        "invalidation_expression": "freshness_state != 'fresh' or last <= 0",
        "long_expression": "return_5m_bps > 0",
        "short_expression": "return_5m_bps < 0",
        "edge_expression": "companion_return_strength_bps",
        "score_expression": "clip(45 + companion_return_strength_bps / 4, 0, 100)",
        "route_surface": "proxy",
    }


def b3_cbio_companion_observation(
    price: float,
    observed_at: str,
    *,
    inst_id: str = "B3:PUBLIC_DATA_SURFACE:CBIO",
    symbol: str = "CBIO",
    **overrides: object,
) -> dict:
    row = {
        "inst_id": inst_id,
        "venue": "B3",
        "trade_type": "official_market_catalog",
        "market_type": "otc_environmental_reference",
        "asset_class": "decarbonization_credit",
        "quote": "USD",
        "base": "CBIO",
        "symbol": symbol,
        "last": price,
        "spread_bps": 3.0,
        "liquidity_score": 0.7,
        "quality_score": 80.0,
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "price_available": True,
        "price_basis": "public_companion_global_carbon_etf_quote",
        "market_surface": "b3_cbio_public_data",
        "freshness_state": "fresh",
        "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
        "stale_minutes": 1.0,
        "session_status": "unknown",
        "observed_at": observed_at,
        "price_source": "TradingView public carbon ETF companion quote",
        "source_url": "https://www.tradingview.com/symbols/NYSEARCA-KRBN/",
        "source_contract_url": "https://www.b3.com.br/en_us/b3/esg/otc-market.htm",
        "companion_quote_symbol": "KRBN",
        "price_reference_role": "listed_carbon_market_proxy",
        "proxy_symbol": "NYSEARCA:KRBN",
    }
    row.update(overrides)
    return row


def b3_cbio_companion_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["B3"],
            "trade_types": ["official_market_catalog"],
            "market_surfaces": ["b3_cbio_public_data"],
        },
        "calculated_features": {
            "carbon_proxy_return_strength_bps": "abs(return_5m_bps)",
        },
        "entry_expression": (
            "market_surface == 'b3_cbio_public_data' "
            "and price_basis == 'public_companion_global_carbon_etf_quote' "
            "and quality_status == 'verified_proxy' and freshness_state == 'fresh' "
            "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
            "and last > 0"
        ),
        "invalidation_expression": "freshness_state != 'fresh' or last <= 0",
        "long_expression": "return_5m_bps > 0",
        "short_expression": "return_5m_bps < 0",
        "edge_expression": "carbon_proxy_return_strength_bps",
        "score_expression": "clip(45 + carbon_proxy_return_strength_bps / 4, 0, 100)",
        "route_surface": "proxy",
    }


def boc_auction_observation(
    price: float,
    auction_at: dt.datetime,
    *,
    term_days: int = 91,
    average_yield_pct: float = 2.5,
    coverage_ratio: float = 2.2,
    tail_bps: float = 1.0,
    **overrides: object,
) -> dict:
    row = {
        "inst_id": f"BANK_OF_CANADA:TEST:AUCTION:{auction_at.date().isoformat()}:{term_days}",
        "venue": "BANK_OF_CANADA",
        "trade_type": "official_primary_auction_result",
        "market_type": "treasury_bill_auction_reference",
        "asset_class": "sovereign_treasury_bill",
        "quote": "CAD_PER_100_FACE",
        "base": f"CANADA_TBILL_{term_days}",
        "last": price,
        "coverage_ratio": coverage_ratio,
        "tail_bps": tail_bps,
        "term_days": term_days,
        "average_yield_pct": average_yield_pct,
        "stop_out_yield_pct": average_yield_pct + 0.01,
        "quality_status": "official_auction_result",
        "market_surface": "canada_regular_treasury_bill_auctions",
        "freshness_state": "fresh",
        "candidate_reject_reason": "official_auction_result_not_executable_quote",
        "session_status": "results_published",
        "auction_at": auction_at.isoformat(),
        "observed_at": auction_at.isoformat(),
        "price_source": "Bank of Canada Valet AUC_TBILL",
    }
    row.update(overrides)
    return row


def boc_auction_reference_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["BANK_OF_CANADA"],
            "asset_classes": ["sovereign_treasury_bill"],
            "market_types": ["treasury_bill_auction_reference"],
        },
        "calculated_features": {
            "auction_demand_pressure": "auction_coverage_ratio - abs(auction_tail_bps) / 10",
        },
        "entry_expression": (
            "market_surface == 'canada_regular_treasury_bill_auctions' "
            "and quality_status == 'official_auction_result' "
            "and candidate_reject_reason == 'official_auction_result_not_executable_quote' "
            "and freshness_state == 'fresh' and auction_coverage_ratio >= 2 "
            "and abs(auction_tail_bps) <= 2 and auction_result_published >= 1"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or auction_coverage_ratio < 2 "
            "or abs(auction_tail_bps) > 2"
        ),
        "direction": "long",
        "edge_expression": "auction_demand_pressure",
        "score_expression": "clip(50 + 10 * auction_demand_pressure, 0, 100)",
        "route_surface": "auction_reference",
    }


def bahrain_auction_observation(
    price: float,
    published_at: dt.datetime,
    *,
    issue_number: int,
    maturity_days: int = 91,
    average_interest_rate_pct: float = 4.91,
    previous_average_interest_rate_pct: float = 4.90,
    oversubscription_pct: float = 101.0,
    **overrides: object,
) -> dict:
    row = {
        "inst_id": f"CENTRAL_BANK_OF_BAHRAIN:TBILL:ISSUE:{issue_number}",
        "venue": "CENTRAL_BANK_OF_BAHRAIN",
        "trade_type": "official_primary_auction_result",
        "market_type": "treasury_bill_auction_reference",
        "asset_class": "sovereign_treasury_bill",
        "quote": "BHD_PER_100_FACE",
        "base": f"BAHRAIN_TBILL_{maturity_days}",
        "last": price,
        "maturity_days": maturity_days,
        "term_days": maturity_days,
        "average_interest_rate_pct": average_interest_rate_pct,
        "average_yield_pct": average_interest_rate_pct,
        "previous_average_interest_rate_pct": previous_average_interest_rate_pct,
        "average_price_per_100": price,
        "lowest_accepted_price_per_100": price - 0.046,
        "oversubscription_pct": oversubscription_pct,
        "coverage_ratio": 1.0 + (oversubscription_pct / 100.0),
        "quality_status": "official_auction_result",
        "market_surface": "bahrain_government_treasury_bill_auctions",
        "freshness_state": "fresh",
        "candidate_reject_reason": "official_auction_result_not_executable_quote",
        "session_status": "results_published",
        "auction_at": published_at.isoformat(),
        "result_published_date": published_at.date().isoformat(),
        "issue_date": (published_at + dt.timedelta(days=2)).date().isoformat(),
        "observed_at": published_at.isoformat(),
        "price_source": "Central Bank of Bahrain Treasury-bill allotment press release",
    }
    row.update(overrides)
    return row


def bahrain_auction_reference_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["CENTRAL_BANK_OF_BAHRAIN"],
            "asset_classes": ["sovereign_treasury_bill"],
            "market_types": ["treasury_bill_auction_reference"],
            "market_surfaces": ["bahrain_government_treasury_bill_auctions"],
        },
        "calculated_features": {
            "rate_change_bps": "100 * (average_interest_rate_pct - previous_average_interest_rate_pct)",
            "bahrain_demand_pressure": "oversubscription_pct - max(rate_change_bps, 0)",
        },
        "entry_expression": (
            "market_surface == 'bahrain_government_treasury_bill_auctions' "
            "and quality_status == 'official_auction_result' "
            "and candidate_reject_reason == 'official_auction_result_not_executable_quote' "
            "and freshness_state == 'fresh' and oversubscription_pct >= 100 "
            "and average_interest_rate_pct > 0 and previous_average_interest_rate_pct > 0 "
            "and maturity_days > 0 and auction_result_published >= 1"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or average_interest_rate_pct <= 0 "
            "or previous_average_interest_rate_pct <= 0 or maturity_days <= 0"
        ),
        "direction": "long",
        "edge_expression": "bahrain_demand_pressure / 10",
        "score_expression": "clip(45 + bahrain_demand_pressure / 2, 0, 100)",
        "route_surface": "auction_reference",
    }


def aofm_tender_observation(
    average_yield_pct: float,
    auction_at: dt.datetime,
    *,
    isin: str = "AU000XCLWAM8",
    maturity_date_iso: str = "2047-03-21",
    term_days: int = 7514,
    coverage_ratio: float = 2.15,
    coupon_pct: float = 3.0,
    **overrides: object,
) -> dict:
    row = {
        "inst_id": f"AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT:TBOND:RESULT:{isin}:{auction_at.date().isoformat()}",
        "venue": "AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT",
        "trade_type": "official_primary_auction_result",
        "source_trade_type": "official_primary_tender_result",
        "market_type": "sovereign_treasury_bond_tender_result_reference",
        "asset_class": "australian_government_treasury_bond",
        "quote": "AUD_YIELD_PCT",
        "base": isin,
        "symbol": isin,
        "isin": isin,
        "coupon_pct": coupon_pct,
        "maturity_date_iso": maturity_date_iso,
        "last": average_yield_pct,
        "average_yield_pct": average_yield_pct,
        "weighted_average_yield_pct": average_yield_pct,
        "coverage_ratio": coverage_ratio,
        "bid_cover_ratio": coverage_ratio,
        "term_days": term_days,
        "market_surface": "australian_treasury_bond_tenders_and_results",
        "quality_status": "official_auction_result",
        "source_quality_status": "official_tender_result",
        "freshness_state": "fresh",
        "candidate_reject_reason": "official_auction_result_not_executable_quote",
        "session_status": "results_published",
        "auction_at": auction_at.isoformat(),
        "observed_at": auction_at.isoformat(),
        "price_source": "AOFM Treasury Bonds issuance Data Hub workbook",
        "source_url": "https://www.aofm.gov.au/data-hub",
    }
    row.update(overrides)
    return row


def aofm_tender_reference_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT"],
            "asset_classes": ["australian_government_treasury_bond"],
            "market_surfaces": ["australian_treasury_bond_tenders_and_results"],
        },
        "calculated_features": {
            "aofm_term_years": "auction_term_days / 365",
            "aofm_demand_pressure": "auction_coverage_ratio - max(auction_average_yield_pct - 4, 0)",
        },
        "entry_expression": (
            "market_surface == 'australian_treasury_bond_tenders_and_results' "
            "and quality_status == 'official_auction_result' "
            "and candidate_reject_reason == 'official_auction_result_not_executable_quote' "
            "and freshness_state == 'fresh' and auction_coverage_ratio >= 2 "
            "and auction_average_yield_pct > 0 and auction_term_days >= 365 "
            "and auction_result_published >= 1"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or auction_coverage_ratio < 1.25 "
            "or auction_average_yield_pct <= 0 or auction_term_days < 365"
        ),
        "direction": "long",
        "edge_expression": "aofm_demand_pressure + min(aofm_term_years / 2, 5)",
        "score_expression": "clip(45 + 10 * aofm_demand_pressure + min(aofm_term_years, 12), 0, 100)",
        "route_surface": "auction_reference",
    }


def carb_allowance_auction_observation(
    price: float,
    event_date: dt.date,
    *,
    auction_number: int,
    allowance_category: str,
    allowances_offered: float,
    allowances_sold: float | None = None,
    **overrides: object,
) -> dict:
    sold = allowances_offered if allowances_sold is None else allowances_sold
    observed_at = dt.datetime.combine(
        event_date,
        dt.time(hour=12, tzinfo=dt.timezone.utc),
    ).isoformat()
    row = {
        "inst_id": (
            f"CARB:CA_QC_AUCTION:{auction_number}:{allowance_category.upper()}:{event_date.isoformat()}"
        ),
        "venue": "CARB_CA_QC",
        "trade_type": "official_market_reference",
        "market_type": "joint_allowance_auction_result",
        "market_surface": "california_quebec_cap_and_invest_joint_allowance_auctions",
        "asset_class": "greenhouse_gas_emission_allowance",
        "quote": "USD_PER_ALLOWANCE",
        "base": "CA_QC_GHG_ALLOWANCE",
        "last": price,
        "price_available": True,
        "auction_settlement_price_usd": price,
        "allowances_offered": allowances_offered,
        "allowances_sold": sold,
        "allowance_category": allowance_category,
        "auction_number": auction_number,
        "event_date": event_date.isoformat(),
        "quality_status": "official_auction_result",
        "freshness_state": "fresh",
        "candidate_reject_reason": "official_allowance_auction_reference_not_order_routable",
        "session_status": "closed",
        "reserve_sale": allowance_category == "reserve",
        "observed_at": observed_at,
        "price_source": "California Air Resources Board public Cap-and-Invest record",
    }
    row.update(overrides)
    return row


def carb_allowance_auction_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["CARB_CA_QC"],
            "asset_classes": ["greenhouse_gas_emission_allowance"],
            "market_types": ["joint_allowance_auction_result"],
            "market_surfaces": ["california_quebec_cap_and_invest_joint_allowance_auctions"],
        },
        "calculated_features": {
            "paired_sellthrough_total": "current_sellthrough + advance_sellthrough",
            "tight_discount_edge_bps": "max(0, 25 - term_discount_bps)",
        },
        "entry_expression": (
            "market_surface == 'california_quebec_cap_and_invest_joint_allowance_auctions' "
            "and quality_status == 'official_auction_result' "
            "and candidate_reject_reason == 'official_allowance_auction_reference_not_order_routable' "
            "and allowance_category == 'current' and paired_current_advance_observed >= 1 "
            "and current_sellthrough >= 1 and advance_sellthrough >= 1 "
            "and price_available == True and reserve_sale == False"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or paired_current_advance_observed < 1 "
            "or reserve_sale == True or price_available == False"
        ),
        "direction": "long",
        "edge_expression": "tight_discount_edge_bps + 10 * max(paired_sellthrough_total - 2, 0)",
        "score_expression": "clip(50 + tight_discount_edge_bps + 5 * paired_current_advance_observed, 0, 100)",
        "route_surface": "auction_reference",
    }


def eex_reported_spot_observation(price: float, observed_at: str, **overrides: object) -> dict:
    row = {
        "inst_id": "EEX:EUAA:SPOT:691200",
        "venue": "EEX",
        "trade_type": "official_market_reference",
        "market_type": "spot_trade_reference",
        "market_surface": "eex_eu_ets_secondary_spot_trades",
        "asset_class": "emission_allowance",
        "base": "EUAA",
        "quote": "EUR_PER_TCO2",
        "last": price,
        "reported_trade_price": price,
        "reported_trade_volume": 1_000.0,
        "reported_trade_valid": 1.0,
        "traded_volume": 1_000.0,
        "trade_id": "691200",
        "quality_status": "official_reported_trade",
        "candidate_reject_reason": "reported_spot_trade_not_executable_quote",
        "freshness_state": "fresh",
        "session_status": "continuous",
        "data_status": "reachable",
        "observed_at": observed_at,
        "price_source": "EEX EU ETS secondary spot trade",
        "source_url": "https://api1.datasource.eex-group.com/getSpot/2026-08-04",
        "source_record_type": "datasource_getSpot",
        "source_adapter_id": "eex_eua_primary_auction_spot_public",
    }
    row.update(overrides)
    return row


def eex_reported_spot_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["EEX"],
            "asset_classes": ["emission_allowance"],
            "market_types": ["spot_trade_reference"],
            "market_surfaces": ["eex_eu_ets_secondary_spot_trades"],
        },
        "calculated_features": {
            "reported_trade_volume_signal": "log1p(reported_trade_volume)",
            "reported_trade_validation_signal": "reported_trade_valid",
        },
        "entry_expression": (
            "quality_status == 'official_reported_trade' "
            "and candidate_reject_reason == 'reported_spot_trade_not_executable_quote' "
            "and freshness_state == 'fresh' and reported_trade_price > 0"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or reported_trade_price <= 0"
        ),
        "direction": "long",
        "edge_expression": "return_5m_bps",
        "score_expression": (
            "clip(50 + return_5m_bps / 4 + reported_trade_volume_signal "
            "+ reported_trade_validation_signal, 0, 100)"
        ),
        "route_surface": "proxy",
    }


def icdx_cpotr_price_card_observation(
    price: float,
    observed_at: str,
    *,
    price_type: str,
    contract_month: str = "AUG26",
    **overrides: object,
) -> dict:
    normalized_type = str(price_type)
    code = "SOBO" if normalized_type == "suggested_opening" else "YDSP"
    row = {
        "inst_id": f"ICDX:CPOTR:{contract_month}:{code}",
        "venue": "ICDX",
        "trade_type": "official_price_card_reference",
        "market_type": "commodity_futures_reference_price",
        "market_surface": "icdx_cpotr",
        "asset_class": "crude_palm_oil_futures",
        "base": "CRUDE_PALM_OIL",
        "quote": "IDR_PER_TONNE",
        "last": price,
        "contract_month": contract_month,
        "price_type": normalized_type,
        "price_basis": (
            "suggested_opening_idr_per_tonne"
            if normalized_type == "suggested_opening"
            else "previous_settlement_idr_per_tonne"
        ),
        "quality_status": "official_price_card",
        "candidate_reject_reason": "public_price_card_not_execution_route",
        "freshness_state": "fresh",
        "session_status": (
            "pre_open_indicative"
            if normalized_type == "suggested_opening"
            else "previous_settlement_reference"
        ),
        "data_status": "reachable",
        "observed_at": observed_at,
        "price_source": "ICDX homepage official price card",
        "source_url": "https://www.icdx.co.id/",
        "source_record_type": "homepage_price_card",
        "source_adapter_id": "indonesia_commodity_derivatives_exchange_icdx",
    }
    row.update(overrides)
    return row


def icdx_cpotr_price_card_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["ICDX"],
            "trade_types": ["official_price_card_reference"],
            "market_surfaces": ["icdx_cpotr"],
        },
        "calculated_features": {
            "cpotr_opening_gap_abs_bps": "abs(cpotr_opening_gap_bps)",
        },
        "entry_expression": (
            "market_surface == 'icdx_cpotr' "
            "and quality_status == 'official_price_card' "
            "and candidate_reject_reason == 'public_price_card_not_execution_route' "
            "and freshness_state == 'fresh' "
            "and cpotr_price_card_pair_observed >= 1 "
            "and price_type == 'previous_settlement' "
            "and previous_settlement_price > 0 and suggested_opening_price > 0"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or cpotr_price_card_pair_observed < 1 "
            "or previous_settlement_price <= 0 or suggested_opening_price <= 0"
        ),
        "long_expression": "cpotr_opening_gap_bps > 0",
        "short_expression": "cpotr_opening_gap_bps < 0",
        "edge_expression": "cpotr_opening_gap_abs_bps",
        "score_expression": (
            "clip(45 + min(cpotr_opening_gap_abs_bps, 80) / 2 "
            "+ 5 * cpotr_price_card_pair_observed, 0, 100)"
        ),
        "route_surface": "proxy",
    }


def icdx_milestone_companion_observation(
    price: float,
    observed_at: str,
    *,
    contract_month: str = "AUG26",
    **overrides: object,
) -> dict:
    row = {
        "inst_id": "ICDX:MARKET_MILESTONES",
        "venue": "ICDX",
        "trade_type": "official_market_milestone_reference",
        "market_type": "exchange_milestone_reference",
        "market_surface": "icdx_exchange_milestones",
        "asset_class": "exchange_development",
        "base": "ICDX",
        "quote": "IDR_PER_TONNE",
        "last": price,
        "contract_month": contract_month,
        "price_type": "previous_settlement",
        "price_basis": "public_companion_cpotr_previous_settlement",
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
        "freshness_state": "fresh",
        "session_status": "previous_settlement_reference",
        "data_status": "reachable",
        "observed_at": observed_at,
        "price_source": "ICDX homepage official price card",
        "source_url": "https://www.icdx.co.id/",
        "source_timeline_url": "https://www.icdx.co.id/about-us",
        "source_record_type": "milestone_timeline_with_homepage_price_card_companion",
        "source_adapter_id": "indonesia_commodity_derivatives_exchange_icdx",
        "companion_inst_id": f"ICDX:CPOTR:{contract_month}:YDSP",
        "proxy_symbol": f"ICDX:CPOTR:{contract_month}:YDSP",
        "exchange_established_year": 2009,
        "cpotr_launch_year": 2010,
        "gofx_launch_year": 2018,
        "gofx_micro_launch_year": 2019,
        "crude_oil_contract_launch_year": 2020,
        "exchange_age_years": 17.0,
        "years_since_cpotr_launch": 16.0,
        "years_since_gofx_launch": 8.0,
        "years_since_gofx_micro_launch": 7.0,
        "years_since_crude_oil_contract_launch": 6.0,
        "cpotr_price_card_pair_observed": 1.0,
        "suggested_opening_price": 16875.0,
        "previous_settlement_price": price,
        "cpotr_opening_gap_bps": ((16875.0 - price) / price) * 10_000.0,
    }
    row.update(overrides)
    return row


def icdx_milestone_companion_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["ICDX"],
            "trade_types": ["official_market_milestone_reference"],
            "market_surfaces": ["icdx_exchange_milestones"],
        },
        "calculated_features": {
            "milestone_reference_depth_years": (
                "years_since_cpotr_launch + years_since_gofx_launch + years_since_crude_oil_contract_launch"
            ),
            "milestone_proxy_gap_abs_bps": "abs(cpotr_opening_gap_bps)",
            "milestone_structural_signal": "min(milestone_reference_depth_years, 40)",
        },
        "entry_expression": (
            "market_surface == 'icdx_exchange_milestones' "
            "and price_basis == 'public_companion_cpotr_previous_settlement' "
            "and quality_status == 'verified_proxy' "
            "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
            "and freshness_state == 'fresh' "
            "and cpotr_price_card_pair_observed >= 1 "
            "and last > 0"
        ),
        "invalidation_expression": "freshness_state != 'fresh' or cpotr_price_card_pair_observed < 1 or last <= 0",
        "long_expression": "cpotr_opening_gap_bps > 0",
        "short_expression": "cpotr_opening_gap_bps < 0",
        "edge_expression": "milestone_proxy_gap_abs_bps + milestone_structural_signal",
        "score_expression": (
            "clip(45 + min(milestone_proxy_gap_abs_bps, 80) / 2 + milestone_structural_signal / 4, 0, 100)"
        ),
        "route_surface": "proxy",
    }


def anp_opc_companion_observation(
    price: float,
    observed_at: str,
    *,
    inst_id: str = "ANP:OPC:NEW_EXPLORATORY_BLOCKS:2026-04-14",
    available_exploratory_blocks: float = 0.0,
    new_exploratory_blocks: float = 45.0,
    offshore_new_blocks: float = 37.0,
    onshore_new_blocks: float = 8.0,
    **overrides: object,
) -> dict:
    row = {
        "inst_id": inst_id,
        "venue": "ANP_BRAZIL_OPC",
        "trade_type": "official_regulatory_programme_reference",
        "market_type": "exploration_block_programme_amendment",
        "market_surface": "anp_oferta_permanente_de_concessao",
        "asset_class": "oil_and_gas_exploration_rights_reference",
        "base": "BRAZIL_EXPLORATION_BLOCKS",
        "quote": "USD_PER_ADR_PROXY",
        "last": price,
        "price_basis": "public_companion_petrobras_adr_quote",
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
        "freshness_state": "fresh",
        "session_status": "public_consultation_announced",
        "data_status": "reachable",
        "observed_at": observed_at,
        "price_source": "TradingView public Petrobras ADR companion quote",
        "source_url": "https://www.tradingview.com/symbols/NYSE-PBR/",
        "source_programme_url": (
            "https://www.gov.br/anp/pt-br/canais_atendimento/imprensa/noticias-comunicados/"
            "oferta-permanente-de-concessao-opc-edital-com-inclusao-de-45-blocos-passara-por-audiencia-publica"
        ),
        "source_adapter_id": "anp_oferta_permanente_de_concessao",
        "companion_quote_symbol": "PBR",
        "proxy_symbol": "NYSE:PBR",
        "available_exploratory_blocks": available_exploratory_blocks,
        "new_exploratory_blocks": new_exploratory_blocks,
        "offshore_new_blocks": offshore_new_blocks,
        "onshore_new_blocks": onshore_new_blocks,
    }
    row.update(overrides)
    return row


def anp_opc_companion_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {
            "venues": ["ANP_BRAZIL_OPC"],
            "trade_types": ["official_regulatory_programme_reference"],
            "market_surfaces": ["anp_oferta_permanente_de_concessao"],
        },
        "calculated_features": {
            "opc_catalogue_depth_signal": "available_exploratory_blocks / 25",
            "opc_new_block_signal": "new_exploratory_blocks * 2",
            "opc_offshore_bias_pct": "offshore_new_blocks / max(new_exploratory_blocks, 1)",
            "opc_reference_intensity": "max(opc_catalogue_depth_signal, opc_new_block_signal)",
        },
        "entry_expression": (
            "market_surface == 'anp_oferta_permanente_de_concessao' "
            "and price_basis == 'public_companion_petrobras_adr_quote' "
            "and quality_status == 'verified_proxy' "
            "and candidate_reject_reason == 'public_companion_price_requires_strategy_logic' "
            "and freshness_state == 'fresh' and last > 0 and opc_reference_intensity > 0"
        ),
        "invalidation_expression": (
            "freshness_state != 'fresh' or last <= 0 or opc_reference_intensity <= 0"
        ),
        "direction": "long",
        "edge_expression": "opc_reference_intensity + 10 * opc_offshore_bias_pct",
        "score_expression": (
            "clip(45 + min(opc_reference_intensity, 80) / 2 + 10 * opc_offshore_bias_pct, 0, 100)"
        ),
        "route_surface": "proxy",
    }


class StrategyProgramTests(unittest.TestCase):
    def test_feature_history_limit_is_applied_before_materialization(self) -> None:
        with memory_db() as conn:
            for index in range(5):
                conn.execute(
                    """
                    insert into strategy_feature_snapshots (
                        bucket_at, observed_at, venue, inst_id, trade_type,
                        last, price_source, features_json
                    ) values (?, ?, 'TEST', 'TEST:ABC', 'test', ?, 'unit', '{}')
                    """,
                    (f"2026-08-01T00:0{index}:00+00:00", f"2026-08-01T00:0{index}:00+00:00", 100 + index),
                )
            history = _load_history(
                conn,
                [("TEST", "TEST:ABC")],
                "2026-08-01T00:00:00+00:00",
                2,
            )

        rows = history[("TEST", "TEST:ABC")]
        self.assertEqual([103.0, 104.0], [row["last"] for row in rows])

    def test_safe_expression_rejects_code_and_attribute_access(self) -> None:
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("__import__('os').system('whoami')", {})
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("last.__class__", {"last": 1.0})
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("10 ** 1000000", {})
        self.assertEqual(12.0, evaluate_expression("clip(last + 2, 0, 20)", {"last": 10.0}))

    def test_output_trade_type_is_bounded_to_the_proxy_shock_reversal_family(self) -> None:
        logic = shock_reversal_logic()
        program, diagnostic = compile_observation_program(logic)
        self.assertIsNotNone(program, diagnostic)
        self.assertEqual("global_proxy_shock_reversal", program["output_trade_type"])
        self.assertNotEqual(
            novelty_signature(logic),
            novelty_signature({**logic, "output_trade_type": ""}),
        )

        unsupported = copy.deepcopy(logic)
        unsupported["output_trade_type"] = "model_chosen_family"
        program, diagnostic = compile_observation_program(unsupported)
        self.assertIsNone(program)
        self.assertEqual("unsupported_output_trade_type", diagnostic["reason"])

        wrong_surface = copy.deepcopy(logic)
        wrong_surface["route_surface"] = "spot"
        program, diagnostic = compile_observation_program(wrong_surface)
        self.assertIsNone(program)
        self.assertEqual("output_trade_type_requires_proxy_route_surface", diagnostic["reason"])

        mislabeled_continuation = copy.deepcopy(logic)
        mislabeled_continuation["long_expression"] = "return_60m_bps > 0 and return_5m_bps > 0"
        program, diagnostic = compile_observation_program(mislabeled_continuation)
        self.assertIsNone(program)
        self.assertEqual("shock_reversal_invalid_long_expression", diagnostic["reason"])

    def test_perp_funding_output_is_bounded_to_broad_short_carry_programs(self) -> None:
        program, diagnostic = compile_observation_program(funding_capture_logic())
        self.assertIsNotNone(program, diagnostic)
        self.assertEqual("perp_funding_capture", program["output_trade_type"])

        generated, runtime_diagnostic = generate_program_candidates(
            {
                "strategy_lab_id": "missing_basis_history",
                "version": 1,
                "hypothesis": "History is mandatory.",
                "strategy_logic": funding_capture_logic(),
            },
            [
                {
                    **funding_observation(
                        100.0,
                        2.0,
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    ),
                    "basis_history_ready": 0.0,
                    "basis_zscore_60m": 0.0,
                    "basis_volatility_60m_bps": 0.0,
                    "basis_change_5m_bps": 0.0,
                }
            ],
            settings(),
        )
        self.assertEqual([], generated)
        self.assertEqual(1, runtime_diagnostic["reject_reasons"]["entry_expression_false"])

        pinned = copy.deepcopy(funding_capture_logic())
        pinned["universe"]["inst_ids"] = ["BTC-USDT-SWAP"]
        program, diagnostic = compile_observation_program(pinned)
        self.assertIsNone(program)
        self.assertEqual("perp_funding_capture_must_not_pin_instruments", diagnostic["reason"])

        wrong_direction = copy.deepcopy(funding_capture_logic())
        wrong_direction["direction"] = "long"
        program, diagnostic = compile_observation_program(wrong_direction)
        self.assertIsNone(program)
        self.assertEqual("perp_funding_capture_requires_short_direction", diagnostic["reason"])

    def test_calculated_feature_dependencies_ignore_serialized_key_order(self) -> None:
        logic = json.loads(json.dumps(shock_reversal_logic(), sort_keys=True))
        self.assertEqual(
            "cost_adjusted_reversal_edge_bps",
            next(iter(logic["calculated_features"])),
        )

        program, diagnostic = compile_observation_program(logic)

        self.assertIsNotNone(program, diagnostic)
        calculated_names = list(program["calculated_features"])
        self.assertLess(
            calculated_names.index("flip_strength_bps"),
            calculated_names.index("cost_adjusted_reversal_edge_bps"),
        )

    def test_calculated_feature_dependency_cycles_are_invalid(self) -> None:
        logic = program_logic()
        logic["calculated_features"] = {
            "first": "second + 1",
            "second": "first + 1",
        }
        logic["entry_expression"] = "first > 0"
        logic["long_expression"] = "first > 0"

        program, diagnostic = compile_observation_program(logic)

        self.assertIsNone(program)
        self.assertEqual("invalid", diagnostic["status"])
        self.assertEqual(
            "calculated_feature_dependency_cycle:first,second",
            diagnostic["reason"],
        )

    def test_shock_reversal_output_preserves_source_lineage_and_emits_both_sides(self) -> None:
        base = {
            **observation(100.0, dt.datetime.now(dt.timezone.utc).isoformat()),
            "volatility_60m_bps": 20.0,
            "quality_score": 80.0,
            "liquidity_score": 0.8,
            "spread_bps": 2.0,
            "stale_minutes": 1.0,
            "provider_age_seconds": 60.0,
            "quote_volume_24h": 2_000_000.0,
            "data_status": "reachable",
        }
        frames = [
            {**base, "inst_id": "DOWN", "return_60m_bps": -80.0, "return_5m_bps": 10.0},
            {**base, "inst_id": "UP", "return_60m_bps": 80.0, "return_5m_bps": -10.0},
            {**base, "inst_id": "NO_FLIP", "return_60m_bps": 80.0, "return_5m_bps": 10.0},
        ]
        experiment = {
            "strategy_lab_id": "shock_reversal",
            "version": 1,
            "hypothesis": "Extreme proxy shocks reverse after a five-minute flip.",
            "strategy_logic": shock_reversal_logic(),
        }

        generated, diagnostic = generate_program_candidates(experiment, frames, settings())

        self.assertEqual(2, len(generated), diagnostic)
        self.assertEqual({"long_proxy", "short_proxy"}, {row["direction"] for row in generated})
        self.assertTrue(all(row["trade_type"] == "global_proxy_shock_reversal" for row in generated))
        self.assertTrue(all(row["strategy_lab_source_trade_type"] == "global_market_discovery_proxy" for row in generated))
        self.assertTrue(all(row["proxy_depth_notional_usd"] == 2_000_000.0 for row in generated))
        self.assertTrue(all(row["freshness_age_seconds"] == 60.0 for row in generated))

    def test_snapshot_store_uses_five_minute_buckets_and_enforces_cap(self) -> None:
        cfg = settings()
        cfg["strategy_lab"]["feature_snapshot_max_rows"] = 2
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        with memory_db() as conn:
            for offset, price in ((-15, 100.0), (-10, 101.0), (-5, 102.0)):
                record_feature_snapshots(
                    conn,
                    [observation(price, (now + dt.timedelta(minutes=offset)).isoformat())],
                    cfg,
                )
            rows = conn.execute(
                "select bucket_at, last from strategy_feature_snapshots order by bucket_at"
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual([101.0, 102.0], [row["last"] for row in rows])
        self.assertTrue(all(dt.datetime.fromisoformat(row["bucket_at"]).minute % 5 == 0 for row in rows))

    def test_gapped_snapshot_history_cannot_create_short_horizon_signal(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        with memory_db() as conn:
            record_feature_snapshots(
                conn,
                [observation(100.0, (now - dt.timedelta(minutes=10)).isoformat())],
                cfg,
            )
            frames, summary = record_feature_snapshots(
                conn,
                [observation(110.0, now.isoformat())],
                cfg,
            )
            generated, diagnostic = generate_program_candidates(
                {
                    "strategy_lab_id": "gapped_history_v1",
                    "version": 1,
                    "hypothesis": "A five-minute return requires the immediately prior bucket.",
                    "strategy_logic": program_logic(),
                },
                frames,
                cfg,
            )

        self.assertTrue(summary["enabled"])
        self.assertEqual(0, frames[0]["feature_history_contiguous_points"])
        self.assertEqual(0.0, frames[0]["return_5m_bps"])
        self.assertEqual([], generated)
        self.assertEqual(1, diagnostic["reject_reasons"]["feature_history_not_contiguous"])

    def test_snapshot_input_caps_are_venue_balanced_and_instrument_bounded(self) -> None:
        cfg = settings()
        cfg["strategy_lab"]["snapshot_max_inputs_per_loop"] = 3
        cfg["strategy_lab"]["snapshot_max_instruments_per_loop"] = 3
        observed_at = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0).isoformat()
        rows = []
        for venue, inst_ids in (("A", ("A1", "A2", "A3")), ("B", ("B1", "B2", "B3"))):
            for inst_id in inst_ids:
                rows.append(
                    {
                        **observation(100.0, observed_at),
                        "venue": venue,
                        "inst_id": inst_id,
                    }
                )
        with memory_db() as conn:
            frames, summary = record_feature_snapshots(conn, rows, cfg)
            stored = conn.execute(
                "select venue, inst_id from strategy_feature_snapshots order by venue, inst_id"
            ).fetchall()

        self.assertEqual(3, len(frames))
        self.assertEqual(3, summary["input_rows_selected"])
        self.assertEqual(3, summary["instruments_selected"])
        self.assertEqual({"A", "B"}, {row["venue"] for row in stored})

    def test_missing_basis_snapshots_do_not_become_ready_zero_history(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        with memory_db() as conn:
            for index in range(12):
                missing_basis = funding_observation(
                    100.0,
                    2.0,
                    (now - dt.timedelta(minutes=60 - index * 5)).isoformat(),
                )
                missing_basis.pop("basis_bps")
                record_feature_snapshots(conn, [missing_basis], cfg)
            frames, _ = record_feature_snapshots(
                conn,
                [funding_observation(100.0, 2.0, now.isoformat())],
                cfg,
            )

        self.assertEqual(0.0, frames[0]["basis_history_ready"])
        self.assertEqual(0.0, frames[0]["basis_zscore_60m"])
        self.assertEqual(0.0, frames[0]["basis_volatility_60m_bps"])

    def test_observation_program_generates_without_existing_scanner_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_recommendation())
            record_feature_snapshots(
                conn,
                [observation(100.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                {"TEST:ABC": observation(101.0, now.isoformat())},
            )
            row = conn.execute(
                "select compile_status, novelty_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("observation_momentum_v1",),
            ).fetchone()
        self.assertEqual(1, len(generated), report)
        self.assertEqual("observation_program", generated[0]["strategy_lab_logic_type"])
        self.assertEqual("long_proxy", generated[0]["direction"])
        self.assertEqual("global_market_discovery_proxy", generated[0]["trade_type"])
        self.assertGreater(generated[0]["edge_bps_estimate"], 90)
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("novel", row["novelty_status"])
        self.assertEqual(0, report["source_candidate_count"])

    def test_funding_program_uses_basis_history_and_preserves_explicit_route_contract(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = {
            "recommendation_id": "rec_okx_observation_funding",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "strategy_lab_experiment": {
                    "strategy_lab_id": "okx_observation_funding_stability_v1",
                    "version": 1,
                    "experiment_type": "market_strategy",
                    "hypothesis": "Persistent positive funding with stable basis survives costs.",
                    "source_surface": "perp_funding_basis",
                    "permitted_target_surface": ["perp_funding_basis"],
                    "strategy_logic": funding_capture_logic(),
                    "data_requirements": {"paper_only": True},
                    "risk_gates": {"paper_allocation_multiplier": 0.25},
                    "promotion_rules": {"promote_min_labels": 30},
                },
            },
        }
        source_candidate = {
            **funding_observation(100.0, 2.0, now.isoformat()),
            "seen_at": now.isoformat(),
            "direction": "funding_capture_short_perp",
            "score": 80.0,
            "target_surface": "perp_funding_basis",
            "hedge_venue": "OKX_SPOT",
            "hedge_instrument": "BTC-USDT",
            "fee_model": "paper_conservative_v1",
            "paper_leg_mapping_valid": True,
            "route_status": "standard",
        }
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            for index in range(12):
                record_feature_snapshots(
                    conn,
                    [
                        funding_observation(
                            100.0,
                            2.0,
                            (now - dt.timedelta(minutes=60 - index * 5)).isoformat(),
                        )
                    ],
                    cfg,
                )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [source_candidate],
                [funding_observation(100.0, 2.0, now.isoformat())],
            )
            row = conn.execute(
                """
                select status, compile_status, novelty_status
                from strategy_lab_experiments where strategy_lab_id = ?
                """,
                ("okx_observation_funding_stability_v1",),
            ).fetchone()

        self.assertEqual(1, len(generated), report)
        candidate = generated[0]
        self.assertEqual("perp_funding_basis", candidate["trade_type"])
        self.assertEqual("funding_capture_short_perp", candidate["direction"])
        self.assertEqual("perp_funding_basis", candidate["target_surface"])
        self.assertEqual("OKX_SPOT", candidate["hedge_venue"])
        self.assertEqual("BTC-USDT", candidate["hedge_instrument"])
        self.assertEqual("paper_conservative_v1", candidate["fee_model"])
        self.assertTrue(candidate["paper_leg_mapping_valid"])
        self.assertTrue(candidate["paper_only"])
        self.assertEqual(1.0, candidate["strategy_lab_program_features"]["basis_history_ready"])
        self.assertEqual(0.0, candidate["strategy_lab_program_features"]["basis_change_5m_bps"])
        self.assertEqual("active_testing", row["status"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("novel", row["novelty_status"])

    def test_explicit_whitebit_surface_is_preserved_for_verified_paper_programs(self) -> None:
        cfg = settings()
        cfg["account_capabilities"]["crypto_derivatives"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        logic = {
            "type": "observation_program",
            "universe": {
                "venues": ["WHITEBIT"],
                "market_types": ["perp"],
                "market_surfaces": ["whitebit_public_perpetuals"],
            },
            "calculated_features": {
                "cost_adjusted_momentum_bps": "return_5m_bps - spread_bps",
            },
            "entry_expression": (
                "market_surface == 'whitebit_public_perpetuals' and quality_status == 'verified' "
                "and quality_score >= 60 and cost_adjusted_momentum_bps > 5"
            ),
            "invalidation_expression": "quality_status != 'verified' or stale_minutes > 5",
            "direction": "long",
            "edge_expression": "cost_adjusted_momentum_bps",
            "score_expression": "clip(50 + cost_adjusted_momentum_bps / 2, 0, 100)",
            "route_surface": "perp",
        }
        recommendation = lab_recommendation("whitebit_quality_momentum_v1", logic)
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["source_surface"] = "whitebit_public_perpetuals"
        experiment["permitted_target_surface"] = ["whitebit_public_perpetuals"]
        verified = {
            **funding_observation(101.0, 0.0, now.isoformat()),
            "inst_id": "WHITEBIT:BTC_PERP",
            "venue": "WHITEBIT",
            "market_type": "perp",
            "asset_class": "crypto_derivatives",
            "market_surface": "whitebit_public_perpetuals",
            "quality_status": "verified",
            "quality_score": 90.0,
            "data_status": "reachable",
        }
        previous = {**verified, "last": 100.0, "observed_at": (now - dt.timedelta(minutes=5)).isoformat()}

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(conn, [previous], cfg)
            generated, report = generate_strategy_lab_candidates(conn, cfg, [], [verified])

        self.assertEqual(1, len(generated), report)
        self.assertEqual("whitebit_public_perpetuals", generated[0]["source_surface"])
        self.assertEqual("whitebit_public_perpetuals", generated[0]["target_surface"])
        self.assertEqual("frontier_crypto_perp_paper", generated[0]["route_id"])
        self.assertEqual("standard", generated[0]["route_status"])

        unknown_quality = {**verified, "quality_status": "unknown", "quality_score": None}
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(conn, [previous], cfg)
            generated, _report = generate_strategy_lab_candidates(conn, cfg, [], [unknown_quality])
        self.assertEqual([], generated)

    def test_available_observations_with_unmatched_universe_request_contract_repair(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        logic = funding_capture_logic()
        logic["universe"]["market_types"] = ["perp"]
        recommendation = lab_recommendation("okx_runtime_contract_mismatch_v1", logic)
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["source_surface"] = "perp_funding_basis"
        experiment["permitted_target_surface"] = ["perp_funding_basis"]
        observation_row = funding_observation(100.0, 2.0, now.isoformat())

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [observation_row],
            )
            row = conn.execute(
                "select status, evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("okx_runtime_contract_mismatch_v1",),
            ).fetchone()

        self.assertEqual([], generated)
        self.assertEqual(
            "needs_contract_revision",
            report["status_by_experiment"]["okx_runtime_contract_mismatch_v1"],
        )
        self.assertEqual("needs_contract_revision", row["status"])
        mismatch = json.loads(row["evaluation_json"])["generation_diagnostic"][
            "runtime_contract_mismatch"
        ]
        self.assertTrue(mismatch["repairable"])
        self.assertEqual("repair_runtime_contract", mismatch["owner_objective"])
        market_type = next(
            item for item in mismatch["mismatches"] if item["runtime_field"] == "market_type"
        )
        self.assertEqual(["PERP"], market_type["required_values"])
        self.assertEqual(["<MISSING>"], market_type["observed_values"])

    def test_contract_repair_uses_feasibility_when_generator_diagnostic_is_empty(self) -> None:
        mismatch = _runtime_universe_contract_mismatch(
            {"universe": {"venues": ["OKX"], "market_types": ["perp"]}},
            [{"venue": "OKX", "market_type": None}],
            {},
            {"feasibility_status": "missing_surface_data", "universe_match_count": 0},
        )

        self.assertTrue(mismatch["repairable"])
        self.assertEqual("market_type", mismatch["mismatches"][0]["runtime_field"])

    def test_entry_invalidation_overlap_is_tested_and_flagged_for_repair(self) -> None:
        cfg = settings()
        logic = program_logic()
        logic["entry_expression"] = "quality_score >= 60"
        logic["invalidation_expression"] = "quality_score >= 60"
        logic["long_expression"] = "quality_score >= 60"
        logic["edge_expression"] = "1"
        recommendation = lab_recommendation("entry_invalidation_overlap_v1", logic)
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [observation(99.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [observation(100.0, now.isoformat())],
            )
            row = conn.execute(
                "select status, evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("entry_invalidation_overlap_v1",),
            ).fetchone()

        self.assertEqual(1, len(generated))
        self.assertEqual("active_testing", row["status"])
        self.assertEqual(
            "active_testing",
            report["status_by_experiment"]["entry_invalidation_overlap_v1"],
        )
        self.assertTrue(generated[0]["strategy_lab_invalidation_active_at_entry"])
        self.assertEqual("entry_invalidation_overlap", generated[0]["strategy_lab_contract_warning"])
        self.assertFalse(generated[0]["promotion_eligible"])
        mismatch = json.loads(row["evaluation_json"])["generation_diagnostic"][
            "runtime_contract_mismatch"
        ]
        self.assertEqual("entry_invalidation_overlap", mismatch["reason"])
        self.assertTrue(mismatch["repairable"])
        self.assertTrue(mismatch["paper_testing_continues"])
        self.assertEqual("repair_runtime_contract", mismatch["owner_objective"])

    def test_exploration_keeps_route_blocked_lab_candidate_for_synthetic_paper(self) -> None:
        candidate = {
            "inst_id": "BTC-USDT-SWAP",
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "funding_capture_short_perp",
            "paper_route_eligibility": {
                "applies": True,
                "suppressed": True,
                "missing_prerequisites": ["account_permission"],
                "blocker_reasons": ["route_not_confirmed"],
            },
        }

        eligible, blocked, missing, blockers = _paper_route_eligible_candidates(
            [candidate], settings()
        )

        self.assertEqual(1, len(eligible))
        self.assertEqual(1, len(blocked))
        self.assertFalse(eligible[0]["promotion_eligible"])
        self.assertEqual("diagnose", eligible[0]["_hunter_bucket"])
        self.assertEqual(1, missing["account_permission"])
        self.assertEqual(1, blockers["route_not_confirmed"])

    def test_adx_nav_reference_is_same_surface_and_bypasses_execution_orders(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation("adx_nav_reference_v1", adx_nav_reference_logic())
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = "Fresh official ETF NAV updates support isolated paper-reference measurement."
        experiment["source_surface"] = "adx_official_etf_factsheet_nav"
        experiment["permitted_target_surface"] = ["adx_official_etf_factsheet_nav"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [adx_nav_observation(100.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [adx_nav_observation(101.0, now.isoformat())],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("adx_official_etf_factsheet_nav", candidate["target_surface"])
            self.assertTrue(candidate["paper_nav_reference"])
            self.assertTrue(candidate["paper_nav_reference_provenance_valid"])
            self.assertEqual("factsheet_nav_not_entry_quality_quote", candidate["candidate_reject_reason"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertIsNone(execution["order_id"])
            self.assertEqual("paper_reference_labeled", execution["order"]["status"])
            self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

            trade_id = open_paper_trade(
                conn,
                candidate,
                {"learned_score": candidate["score"]},
                execution=execution,
                settings=cfg,
            )
            labeled = conn.execute(
                "select execution_order_id, route_id, candidate_json from paper_trades where id = ?",
                (trade_id,),
            ).fetchone()
            self.assertIsNone(labeled["execution_order_id"])
            self.assertEqual("synthetic_nav_reference_paper", labeled["route_id"])
            self.assertTrue(json.loads(labeled["candidate_json"])["paper_nav_reference"])

        frame = {
            **adx_nav_observation(101.0, now.isoformat()),
            "return_5m_bps": 100.0,
        }
        for invalid in (
            {"freshness_state": "stale"},
            {"session_status": "closed"},
            {"candidate_reject_reason": "unverified_reference"},
        ):
            candidates, diagnostic = generate_program_candidates(
                experiment,
                [{**frame, **invalid}],
                cfg,
            )
            self.assertEqual([], candidates, diagnostic)

    def test_eex_reported_spot_program_preserves_provenance_and_uses_synthetic_paper_route(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "eex_eu_ets_secondary_spot_reported_trade_v1", eex_reported_spot_logic()
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = "Fresh official EEX reported spot trades support isolated continuation measurement."
        experiment["source_surface"] = "eex_eu_ets_secondary_spot_trades"
        experiment["permitted_target_surface"] = ["eex_eu_ets_secondary_spot_trades"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [eex_reported_spot_observation(80.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [eex_reported_spot_observation(80.5, now.isoformat())],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("eex_eu_ets_secondary_spot_trades", candidate["target_surface"])
            self.assertEqual("reported_spot_trade_not_executable_quote", candidate["candidate_reject_reason"])
            self.assertEqual(80.5, candidate["reported_trade_price"])
            self.assertEqual(1_000.0, candidate["reported_trade_volume"])
            self.assertEqual(1.0, candidate["reported_trade_valid"])
            self.assertTrue(candidate["paper_reported_spot_reference"])
            self.assertEqual("synthetic_research_paper", candidate["synthetic_route_id"])
            self.assertEqual(
                "https://api1.datasource.eex-group.com/getSpot/2026-08-04",
                candidate["source_url"],
            )

            synthetic = prepare_candidate_for_exploration(candidate, cfg)
            self.assertTrue(synthetic["synthetic_research_paper"])
            self.assertFalse(synthetic["promotion_eligible"])
            self.assertEqual("synthetic_research_not_live_equivalent", synthetic["paper_execution_semantics"])

            execution = execute_order(
                conn,
                synthetic,
                {"learned_score": synthetic["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("synthetic_research_paper", execution["order"]["route_id"])
            self.assertEqual("paper", execution["order"]["mode"])

    def test_icdx_price_card_program_uses_paired_features_and_synthetic_paper_route(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "icdx_cpotr_price_card_reference_v1",
            icdx_cpotr_price_card_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "A paired ICDX CPOTR suggested opening and previous settlement supports "
            "same-surface synthetic continuation measurement."
        )
        experiment["source_surface"] = "icdx_cpotr"
        experiment["permitted_target_surface"] = ["icdx_cpotr"]

        suggested = icdx_cpotr_price_card_observation(
            16875.0,
            now.isoformat(),
            price_type="suggested_opening",
        )
        settlement = icdx_cpotr_price_card_observation(
            16280.0,
            now.isoformat(),
            price_type="previous_settlement",
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [suggested, settlement],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("icdx_cpotr", candidate["target_surface"])
            self.assertEqual("long_proxy", candidate["direction"])
            self.assertTrue(candidate["paper_cpotr_price_card_reference"])
            self.assertTrue(candidate["paper_cpotr_price_card_provenance_valid"])
            self.assertEqual("synthetic_research_paper", candidate["synthetic_route_id"])
            self.assertEqual(16875.0, candidate["strategy_lab_program_features"]["suggested_opening_price"])
            self.assertEqual(16280.0, candidate["strategy_lab_program_features"]["previous_settlement_price"])
            self.assertEqual(1.0, candidate["strategy_lab_program_features"]["cpotr_price_card_pair_observed"])
            self.assertGreater(candidate["strategy_lab_program_features"]["cpotr_opening_gap_bps"], 0.0)

            synthetic = prepare_candidate_for_exploration(candidate, cfg)
            self.assertTrue(synthetic["synthetic_research_paper"])
            self.assertFalse(synthetic["promotion_eligible"])
            self.assertEqual("synthetic_research_not_live_equivalent", synthetic["paper_execution_semantics"])

            execution = execute_order(
                conn,
                synthetic,
                {"learned_score": synthetic["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("synthetic_research_paper", execution["order"]["route_id"])
            self.assertEqual("paper", execution["order"]["mode"])

    def test_icdx_milestone_program_uses_companion_price_and_synthetic_paper_route(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "icdx_exchange_milestones_companion_v1",
            icdx_milestone_companion_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "ICDX milestone rows can use a CPOTR price-card companion for same-surface synthetic paper testing."
        )
        experiment["source_surface"] = "icdx_exchange_milestones"
        experiment["permitted_target_surface"] = ["icdx_exchange_milestones"]

        milestone = icdx_milestone_companion_observation(
            16280.0,
            now.isoformat(),
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [milestone],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("icdx_exchange_milestones", candidate["target_surface"])
            self.assertEqual("long_proxy", candidate["direction"])
            self.assertTrue(candidate["paper_icdx_milestone_reference"])
            self.assertTrue(candidate["paper_icdx_milestone_provenance_valid"])
            self.assertEqual("synthetic_research_paper", candidate["synthetic_route_id"])
            self.assertGreater(candidate["strategy_lab_program_features"]["cpotr_opening_gap_bps"], 0.0)
            self.assertEqual(16.0, candidate["strategy_lab_program_features"]["years_since_cpotr_launch"])
            self.assertEqual(30.0, candidate["strategy_lab_program_features"]["milestone_reference_depth_years"])

            synthetic = prepare_candidate_for_exploration(candidate, cfg)
            self.assertTrue(synthetic["synthetic_research_paper"])
            self.assertFalse(synthetic["promotion_eligible"])
            self.assertEqual("synthetic_research_not_live_equivalent", synthetic["paper_execution_semantics"])

            execution = execute_order(
                conn,
                synthetic,
                {"learned_score": synthetic["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("synthetic_research_paper", execution["order"]["route_id"])
            self.assertEqual("paper", execution["order"]["mode"])

    def test_anp_opc_program_uses_reference_features_and_synthetic_paper_route(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "anp_opc_brazil_upstream_proxy_v1",
            anp_opc_companion_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Fresh ANP OPC catalogue and amendment intensity can drive same-surface synthetic paper testing with a Petrobras ADR companion quote."
        )
        experiment["source_surface"] = "anp_oferta_permanente_de_concessao"
        experiment["permitted_target_surface"] = ["anp_oferta_permanente_de_concessao"]

        observation = anp_opc_companion_observation(
            14.62,
            now.isoformat(),
            available_exploratory_blocks=495.0,
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [observation],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("anp_oferta_permanente_de_concessao", candidate["target_surface"])
            self.assertEqual("long_proxy", candidate["direction"])
            self.assertTrue(candidate["paper_anp_opc_reference"])
            self.assertTrue(candidate["paper_anp_opc_provenance_valid"])
            self.assertEqual("synthetic_research_paper", candidate["synthetic_route_id"])
            self.assertEqual(45.0, candidate["strategy_lab_program_features"]["new_exploratory_blocks"])
            self.assertEqual(37.0, candidate["strategy_lab_program_features"]["offshore_new_blocks"])
            self.assertEqual(90.0, candidate["strategy_lab_program_features"]["opc_new_block_signal"])
            self.assertGreater(candidate["edge_bps_estimate"], 90.0)

            synthetic = prepare_candidate_for_exploration(candidate, cfg)
            self.assertTrue(synthetic["synthetic_research_paper"])
            self.assertFalse(synthetic["promotion_eligible"])
            self.assertEqual("synthetic_research_not_live_equivalent", synthetic["paper_execution_semantics"])

            execution = execute_order(
                conn,
                synthetic,
                {"learned_score": synthetic["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("synthetic_research_paper", execution["order"]["route_id"])
            self.assertEqual("paper", execution["order"]["mode"])

    def test_adx_derivatives_companion_program_generates_same_surface_proxy_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "adx_derivatives_companion_quote_v1",
            adx_derivatives_companion_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Fresh public companion quotes for ADX single-stock futures underlyings support same-surface paper proxy testing."
        )
        experiment["source_surface"] = "adx_equity_and_index_futures_contract_catalog"
        experiment["permitted_target_surface"] = ["adx_equity_and_index_futures_contract_catalog"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [adx_derivatives_companion_observation(3.30, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [adx_derivatives_companion_observation(3.37, now.isoformat())],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("adx_equity_and_index_futures_contract_catalog", candidate["target_surface"])
            self.assertEqual("global_market_discovery_proxy", candidate["trade_type"])
            self.assertEqual("official_derivatives_contract_reference", candidate["strategy_lab_source_trade_type"])
            self.assertEqual("public_companion_underlying_spot_quote", candidate["price_basis"])
            self.assertEqual("https://www.tradingview.com/symbols/ADX-ADNOCGAS/", candidate["source_url"])
            self.assertEqual("standard", candidate["execution_route"]["route_status"])
            self.assertEqual("equity_proxy_paper", candidate["execution_route"]["route_id"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("paper_filled", execution["order"]["status"])
            self.assertEqual("equity_proxy_paper", execution["order"]["route_id"])

    def test_b3_bdr_etf_companion_program_generates_same_surface_proxy_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "b3_bdr_etf_companion_quote_v1",
            b3_bdr_etf_companion_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Fresh B3 BDR ETF catalog availability paired with a Brazil ETF companion quote supports same-surface paper proxy testing."
        )
        experiment["source_surface"] = "b3_bdr_etf_public_data"
        experiment["permitted_target_surface"] = ["b3_bdr_etf_public_data"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [b3_bdr_etf_companion_observation(29.40, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [b3_bdr_etf_companion_observation(29.88, now.isoformat())],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("b3_bdr_etf_public_data", candidate["target_surface"])
            self.assertEqual("global_market_discovery_proxy", candidate["trade_type"])
            self.assertEqual("official_market_catalog", candidate["strategy_lab_source_trade_type"])
            self.assertEqual("public_companion_brazil_equity_etf_quote", candidate["price_basis"])
            self.assertEqual("https://www.tradingview.com/symbols/AMEX-EWZ/", candidate["source_url"])
            self.assertEqual("https://www.b3.com.br/pt_br/bdr-etf.htm", candidate["source_contract_url"])
            self.assertEqual("standard", candidate["execution_route"]["route_status"])
            self.assertEqual("equity_proxy_paper", candidate["execution_route"]["route_id"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("paper_filled", execution["order"]["status"])
            self.assertEqual("equity_proxy_paper", execution["order"]["route_id"])

    def test_b3_cbio_companion_program_generates_same_surface_proxy_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        recommendation = lab_recommendation(
            "b3_cbio_companion_quote_v1",
            b3_cbio_companion_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Fresh B3 CBIO catalog availability paired with a carbon ETF companion quote supports same-surface paper proxy testing."
        )
        experiment["source_surface"] = "b3_cbio_public_data"
        experiment["permitted_target_surface"] = ["b3_cbio_public_data"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [b3_cbio_companion_observation(30.90, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [b3_cbio_companion_observation(31.42, now.isoformat())],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("b3_cbio_public_data", candidate["target_surface"])
            self.assertEqual("global_market_discovery_proxy", candidate["trade_type"])
            self.assertEqual("official_market_catalog", candidate["strategy_lab_source_trade_type"])
            self.assertEqual("public_companion_global_carbon_etf_quote", candidate["price_basis"])
            self.assertEqual("https://www.tradingview.com/symbols/NYSEARCA-KRBN/", candidate["source_url"])
            self.assertEqual("https://www.b3.com.br/en_us/b3/esg/otc-market.htm", candidate["source_contract_url"])
            self.assertEqual("standard", candidate["execution_route"]["route_status"])
            self.assertEqual("equity_proxy_paper", candidate["execution_route"]["route_id"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("paper_filled", execution["order"]["status"])
            self.assertEqual("equity_proxy_paper", execution["order"]["route_id"])

    def test_boc_auction_reference_uses_feature_snapshots_and_next_same_tenor_label(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        cfg["learning"]["horizon_minutes"] = [0]
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        entry_auction_at = now - dt.timedelta(days=6)
        next_auction_at = now - dt.timedelta(days=1)
        recommendation = lab_recommendation("boc_auction_reference_v1", boc_auction_reference_logic())
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = "Strong, low-tail official bill auctions predict lower next same-tenor yields."
        experiment["source_surface"] = "canada_regular_treasury_bill_auctions"
        experiment["permitted_target_surface"] = ["canada_regular_treasury_bill_auctions"]
        entry = boc_auction_observation(
            99.40,
            entry_auction_at,
            average_yield_pct=2.50,
            coverage_ratio=2.25,
            tail_bps=1.0,
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(conn, cfg, [], [entry])

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("canada_regular_treasury_bill_auctions", candidate["target_surface"])
            self.assertTrue(candidate["paper_auction_reference"])
            self.assertTrue(candidate["paper_auction_reference_provenance_valid"])
            self.assertTrue(candidate["synthetic_research_paper"])
            self.assertEqual(2.25, candidate["strategy_lab_program_features"]["auction_coverage_ratio"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertIsNone(execution["order_id"])
            self.assertEqual("synthetic_auction_reference_paper", execution["order"]["route_id"])
            self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

            live_cfg = copy.deepcopy(cfg)
            live_cfg["mode"] = "live"
            live_execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                live_cfg,
            )
            self.assertFalse(live_execution["paper_filled"])
            self.assertEqual("paper_reference_rejected", live_execution["order"]["status"])
            self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

            trade_id = open_paper_trade(
                conn,
                candidate,
                {"learned_score": candidate["score"]},
                execution=execution,
                settings=cfg,
            )
            self.assertEqual([], record_due_horizon_outcomes(conn, {entry["inst_id"]: entry}, cfg))

            same_auction = {**entry, "average_yield_pct": 1.50}
            wrong_tenor = boc_auction_observation(
                99.45,
                next_auction_at,
                term_days=28,
                average_yield_pct=1.0,
            )
            stale_same_tenor = boc_auction_observation(
                99.45,
                entry_auction_at + dt.timedelta(days=2),
                average_yield_pct=1.0,
                freshness_state="stale",
            )
            next_same_tenor = boc_auction_observation(
                99.50,
                next_auction_at,
                average_yield_pct=2.40,
            )
            recorded = record_due_horizon_outcomes(
                conn,
                {
                    same_auction["inst_id"]: same_auction,
                    wrong_tenor["inst_id"]: wrong_tenor,
                    stale_same_tenor["inst_id"]: stale_same_tenor,
                    next_same_tenor["inst_id"]: next_same_tenor,
                },
                cfg,
            )
            self.assertEqual(1, len(recorded))
            self.assertEqual("valid_auction_event", recorded[0]["measurement_status"])
            self.assertGreater(recorded[0]["pnl_bps"], 0)

            outcome = conn.execute(
                "select price, pnl_bps, measurement_status, context_json from paper_trade_outcomes where trade_id = ?",
                (trade_id,),
            ).fetchone()
            self.assertEqual(2.40, outcome["price"])
            self.assertEqual("valid_auction_event", outcome["measurement_status"])
            context = json.loads(outcome["context_json"])["paper_auction_reference_outcome"]
            self.assertEqual(next_same_tenor["inst_id"], context["outcome_inst_id"])
            self.assertEqual(next_auction_at.isoformat(), context["outcome_auction_at"])

        invalid = {
            **entry,
            "coverage_ratio": 1.99,
            "auction_coverage_ratio": 1.99,
            "auction_tail_bps": 1.0,
            "auction_term_days": 91.0,
            "auction_average_yield_pct": 2.5,
            "auction_stop_out_yield_pct": 2.51,
            "auction_result_published": 1.0,
        }
        candidates, diagnostic = generate_program_candidates(experiment, [invalid], cfg)
        self.assertEqual([], candidates, diagnostic)
        self.assertEqual(1, diagnostic["reject_reasons"]["entry_expression_false"])

    def test_bahrain_auction_reference_uses_normalized_fields_and_next_same_maturity_label(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        cfg["learning"]["horizon_minutes"] = [0]
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        entry_published_at = now - dt.timedelta(days=7)
        next_published_at = now - dt.timedelta(days=1)
        recommendation = lab_recommendation(
            "bahrain_auction_reference_v1",
            bahrain_auction_reference_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Strongly oversubscribed Bahrain Treasury-bill results with limited rate step-up "
            "predict lower next same-maturity average yields."
        )
        experiment["source_surface"] = "bahrain_government_treasury_bill_auctions"
        experiment["permitted_target_surface"] = ["bahrain_government_treasury_bill_auctions"]
        entry = bahrain_auction_observation(
            98.773,
            entry_published_at,
            issue_number=2099,
            average_interest_rate_pct=4.91,
            previous_average_interest_rate_pct=4.90,
            oversubscription_pct=101.0,
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(conn, cfg, [], [entry])

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual(
                "bahrain_government_treasury_bill_auctions",
                candidate["target_surface"],
            )
            self.assertTrue(candidate["paper_auction_reference"])
            self.assertTrue(candidate["paper_auction_reference_provenance_valid"])
            self.assertEqual(
                101.0,
                candidate["strategy_lab_program_features"]["oversubscription_pct"],
            )
            self.assertEqual(
                4.90,
                candidate["strategy_lab_program_features"]["previous_average_interest_rate_pct"],
            )

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual(
                "synthetic_auction_reference_paper",
                execution["order"]["route_id"],
            )

            trade_id = open_paper_trade(
                conn,
                candidate,
                {"learned_score": candidate["score"]},
                execution=execution,
                settings=cfg,
            )
            self.assertEqual([], record_due_horizon_outcomes(conn, {entry["inst_id"]: entry}, cfg))

            same_auction = {**entry, "average_interest_rate_pct": 4.50, "average_yield_pct": 4.50}
            wrong_maturity = bahrain_auction_observation(
                98.820,
                next_published_at,
                issue_number=2100,
                maturity_days=182,
                average_interest_rate_pct=4.70,
                previous_average_interest_rate_pct=4.91,
                oversubscription_pct=120.0,
            )
            stale_same_maturity = bahrain_auction_observation(
                98.810,
                entry_published_at + dt.timedelta(days=2),
                issue_number=2101,
                average_interest_rate_pct=4.80,
                previous_average_interest_rate_pct=4.91,
                oversubscription_pct=99.0,
                freshness_state="stale",
            )
            next_same_maturity = bahrain_auction_observation(
                98.805,
                next_published_at,
                issue_number=2102,
                average_interest_rate_pct=4.85,
                previous_average_interest_rate_pct=4.91,
                oversubscription_pct=108.0,
            )
            recorded = record_due_horizon_outcomes(
                conn,
                {
                    same_auction["inst_id"]: same_auction,
                    wrong_maturity["inst_id"]: wrong_maturity,
                    stale_same_maturity["inst_id"]: stale_same_maturity,
                    next_same_maturity["inst_id"]: next_same_maturity,
                },
                cfg,
            )
            self.assertEqual(1, len(recorded))
            self.assertEqual("valid_auction_event", recorded[0]["measurement_status"])
            self.assertGreater(recorded[0]["pnl_bps"], 0)

            outcome = conn.execute(
                "select price, pnl_bps, measurement_status, context_json from paper_trade_outcomes where trade_id = ?",
                (trade_id,),
            ).fetchone()
            self.assertEqual(4.85, outcome["price"])
            self.assertEqual("valid_auction_event", outcome["measurement_status"])
            context = json.loads(outcome["context_json"])["paper_auction_reference_outcome"]
            self.assertEqual(next_same_maturity["inst_id"], context["outcome_inst_id"])
            self.assertEqual(next_published_at.isoformat(), context["outcome_auction_at"])

    def test_aofm_auction_reference_uses_same_isin_for_next_tender_label(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        cfg["learning"]["horizon_minutes"] = [0]
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        entry_auction_at = now - dt.timedelta(days=14)
        next_auction_at = now - dt.timedelta(days=1)
        recommendation = lab_recommendation(
            "aofm_treasury_bond_tender_strength_v1",
            aofm_tender_reference_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Strongly covered AOFM bond tenders tend to be followed by firmer next same-ISIN tender yields."
        )
        experiment["source_surface"] = "australian_treasury_bond_tenders_and_results"
        experiment["permitted_target_surface"] = ["australian_treasury_bond_tenders_and_results"]
        entry = aofm_tender_observation(4.321, entry_auction_at)

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            generated, report = generate_strategy_lab_candidates(conn, cfg, [], [entry])

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual("australian_treasury_bond_tenders_and_results", candidate["target_surface"])
            self.assertTrue(candidate["paper_auction_reference"])
            self.assertTrue(candidate["paper_auction_reference_provenance_valid"])
            self.assertEqual("AU000XCLWAM8", candidate["paper_auction_reference_provenance"]["isin"])
            self.assertEqual(2.15, candidate["strategy_lab_program_features"]["auction_coverage_ratio"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertEqual("synthetic_auction_reference_paper", execution["order"]["route_id"])

            trade_id = open_paper_trade(
                conn,
                candidate,
                {"learned_score": candidate["score"]},
                execution=execution,
                settings=cfg,
            )
            self.assertEqual([], record_due_horizon_outcomes(conn, {entry["inst_id"]: entry}, cfg))

            same_auction = {**entry, "average_yield_pct": 4.10}
            wrong_isin = aofm_tender_observation(
                4.10,
                next_auction_at,
                isin="AU000XCLWAF4",
                maturity_date_iso="2036-10-21",
                term_days=3728,
            )
            stale_same_isin = aofm_tender_observation(
                4.20,
                entry_auction_at + dt.timedelta(days=7),
                term_days=7507,
                freshness_state="stale",
            )
            next_same_isin = aofm_tender_observation(
                4.100,
                next_auction_at,
                term_days=7440,
            )
            recorded = record_due_horizon_outcomes(
                conn,
                {
                    same_auction["inst_id"]: same_auction,
                    wrong_isin["inst_id"]: wrong_isin,
                    stale_same_isin["inst_id"]: stale_same_isin,
                    next_same_isin["inst_id"]: next_same_isin,
                },
                cfg,
            )
            self.assertEqual(1, len(recorded))
            self.assertEqual("valid_auction_event", recorded[0]["measurement_status"])
            self.assertGreater(recorded[0]["pnl_bps"], 0)

            outcome = conn.execute(
                "select price, pnl_bps, measurement_status, context_json from paper_trade_outcomes where trade_id = ?",
                (trade_id,),
            ).fetchone()
            self.assertEqual(4.10, outcome["price"])
            self.assertEqual("valid_auction_event", outcome["measurement_status"])
            context = json.loads(outcome["context_json"])["paper_auction_reference_outcome"]
            self.assertEqual(next_same_isin["inst_id"], context["outcome_inst_id"])
            self.assertEqual(next_auction_at.isoformat(), context["outcome_auction_at"])

    def test_carb_allowance_auction_reference_uses_paired_features_and_next_current_settlement(self) -> None:
        cfg = settings()
        cfg["paper_exploration"]["enabled"] = True
        cfg["learning"]["horizon_minutes"] = [0]
        recommendation = lab_recommendation(
            "carb_allowance_reference_v1",
            carb_allowance_auction_logic(),
        )
        experiment = recommendation["payload"]["strategy_lab_experiment"]
        experiment["hypothesis"] = (
            "Tight current-versus-advance CARB discounts with full sell-through support firmer next current settlements."
        )
        experiment["source_surface"] = "california_quebec_cap_and_invest_joint_allowance_auctions"
        experiment["permitted_target_surface"] = [
            "california_quebec_cap_and_invest_joint_allowance_auctions"
        ]

        prior_date = dt.date(2026, 5, 20)
        entry_date = dt.date(2026, 8, 19)
        next_date = dt.date(2026, 11, 18)
        prior_current = carb_allowance_auction_observation(
            28.95,
            prior_date,
            auction_number=47,
            allowance_category="current",
            allowances_offered=49_647_415.0,
        )
        prior_advance = carb_allowance_auction_observation(
            28.70,
            prior_date,
            auction_number=47,
            allowance_category="advance",
            allowances_offered=6_481_750.0,
        )
        entry_current = carb_allowance_auction_observation(
            28.81,
            entry_date,
            auction_number=48,
            allowance_category="current",
            allowances_offered=51_177_593.0,
        )
        entry_advance = carb_allowance_auction_observation(
            28.76,
            entry_date,
            auction_number=48,
            allowance_category="advance",
            allowances_offered=6_400_000.0,
        )
        next_current = carb_allowance_auction_observation(
            29.10,
            next_date,
            auction_number=49,
            allowance_category="current",
            allowances_offered=50_000_000.0,
        )
        next_advance = carb_allowance_auction_observation(
            28.90,
            next_date,
            auction_number=49,
            allowance_category="advance",
            allowances_offered=6_300_000.0,
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(conn, [prior_current, prior_advance], cfg)
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [entry_current, entry_advance],
            )

            self.assertEqual(1, len(generated), report)
            candidate = generated[0]
            self.assertEqual(
                "california_quebec_cap_and_invest_joint_allowance_auctions",
                candidate["target_surface"],
            )
            self.assertTrue(candidate["paper_auction_reference"])
            self.assertTrue(candidate["paper_auction_reference_provenance_valid"])
            self.assertTrue(candidate["synthetic_research_paper"])
            self.assertEqual("official_allowance_auction_reference_not_order_routable", candidate["candidate_reject_reason"])
            self.assertEqual(1.0, candidate["strategy_lab_program_features"]["paired_current_advance_observed"])
            self.assertEqual(1.0, candidate["strategy_lab_program_features"]["current_sellthrough"])
            self.assertEqual(1.0, candidate["strategy_lab_program_features"]["advance_sellthrough"])
            self.assertGreater(candidate["strategy_lab_program_features"]["term_discount_bps"], 0)
            self.assertEqual(28.81, candidate["strategy_lab_program_features"]["current_price_usd_by_auction"])
            self.assertEqual(28.76, candidate["strategy_lab_program_features"]["advance_price_usd_by_auction"])

            execution = execute_order(
                conn,
                candidate,
                {"learned_score": candidate["score"], "paper_allocation_multiplier": 1.0},
                cfg,
            )
            self.assertTrue(execution["paper_filled"])
            self.assertIsNone(execution["order_id"])
            self.assertEqual("synthetic_auction_reference_paper", execution["order"]["route_id"])
            self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

            trade_id = open_paper_trade(
                conn,
                candidate,
                {"learned_score": candidate["score"]},
                execution=execution,
                settings=cfg,
            )
            self.assertEqual([], record_due_horizon_outcomes(conn, {entry_current["inst_id"]: entry_current}, cfg))

            recorded = record_due_horizon_outcomes(
                conn,
                {
                    next_current["inst_id"]: next_current,
                    next_advance["inst_id"]: next_advance,
                },
                cfg,
            )
            self.assertEqual(1, len(recorded))
            self.assertEqual("valid_auction_event", recorded[0]["measurement_status"])
            self.assertGreater(recorded[0]["pnl_bps"], 0)

            outcome = conn.execute(
                "select price, pnl_bps, measurement_status, context_json from paper_trade_outcomes where trade_id = ?",
                (trade_id,),
            ).fetchone()
            self.assertEqual(29.10, outcome["price"])
            self.assertEqual("valid_auction_event", outcome["measurement_status"])
            context = json.loads(outcome["context_json"])["paper_auction_reference_outcome"]
            self.assertEqual(next_current["inst_id"], context["outcome_inst_id"])
            self.assertEqual(next_date.isoformat(), context["outcome_event_date"])
            self.assertEqual(49, context["outcome_auction_number"])

    def test_exploration_tests_non_positive_model_edge_without_promoting_it(self) -> None:
        cfg = settings()
        logic = program_logic()
        logic["entry_expression"] = "quality_score >= 60"
        logic["long_expression"] = "quality_score >= 60"
        logic["edge_expression"] = "-5"
        recommendation = lab_recommendation("non_positive_edge_exploration_v1", logic)
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            record_feature_snapshots(
                conn,
                [observation(99.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [observation(100.0, now.isoformat())],
            )

        self.assertEqual(1, len(generated))
        self.assertEqual("active_testing", report["status_by_experiment"]["non_positive_edge_exploration_v1"])
        self.assertEqual(-5.0, generated[0]["edge_bps_estimate"])
        self.assertTrue(generated[0]["strategy_lab_non_positive_edge_at_entry"])
        self.assertIn("non_positive_cost_adjusted_edge", generated[0]["strategy_lab_contract_warnings"])
        self.assertFalse(generated[0]["promotion_eligible"])

    def test_partial_invalidation_is_not_a_contract_mismatch(self) -> None:
        mismatch = _runtime_entry_invalidation_contract_mismatch(
            {"candidate_count": 3},
            {"reject_reasons": {"invalidation_expression_true": 2}},
            0,
        )
        self.assertIsNone(mismatch)

    def test_universe_repair_is_not_hidden_by_missing_expression_features(self) -> None:
        mismatch = _runtime_universe_contract_mismatch(
            {"universe": {"market_types": ["perp"]}},
            [{"market_type": None}],
            {"missing_features": ["funding_history_count"]},
            {"feasibility_status": "missing_surface_data", "universe_match_count": 0},
        )

        self.assertTrue(mismatch["repairable"])
        self.assertEqual(["funding_history_count"], mismatch["missing_features"])

    def test_joint_universe_contract_mismatch_across_different_rows(self) -> None:
        mismatch = _runtime_universe_contract_mismatch(
            {
                "universe": {
                    "venues": ["OKX"],
                    "market_types": ["perp"],
                    "trade_types": ["perp_funding_basis"],
                }
            },
            [
                {"venue": "OKX", "market_type": "spot", "trade_type": "perp_funding_basis"},
                {"venue": "OTHER", "market_type": "perp", "trade_type": "perp_funding_basis"},
            ],
            {"reject_reasons": {"universe_mismatch": 2}},
            {"feasibility_status": "missing_surface_data", "universe_match_count": 0},
        )

        self.assertEqual("joint_contract", mismatch["mismatches"][0]["universe_key"])
        self.assertEqual(["market_type"], mismatch["nearest_observations"][0]["failed_fields"])

    def test_runtime_contract_falls_back_to_persisted_logic(self) -> None:
        raw_logic = {"universe": {"market_types": ["perp"]}}

        self.assertEqual(raw_logic, _runtime_contract_program({}, raw_logic))

    def test_program_input_join_does_not_copy_cached_route_eligibility(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        source = {
            **funding_observation(100.0, 2.0, now),
            "direction": "funding_capture_short_perp",
            "paper_route_eligibility": {"suppressed": False, "route_eligible": True},
            "fee_model": "explicit_fixture",
        }
        rows = _observation_program_inputs(
            [funding_observation(100.0, 2.0, now)],
            [source],
        )

        embedded = rows[0]["candidate"]
        self.assertEqual("explicit_fixture", embedded["fee_model"])
        self.assertNotIn("paper_route_eligibility", embedded)
        self.assertNotIn("hedge_venue", embedded)
        self.assertNotIn("paper_leg_mapping_valid", embedded)

    def test_program_inputs_normalize_only_untyped_crypto_spot_rows(self) -> None:
        rows = _observation_program_inputs(
            [
                {
                    "venue": "OKX_SPOT",
                    "inst_id": "OKX_SPOT:BTC-USDT",
                    "market_type": "spot",
                    "asset_class": "crypto_spot",
                    "last": 100.0,
                },
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT-SWAP",
                    "market_type": "perp",
                    "asset_class": "crypto_derivatives",
                    "last": 100.0,
                },
                {
                    "venue": "FIXTURE",
                    "inst_id": "FIXTURE:BTC-USDT",
                    "market_type": "spot",
                    "asset_class": "crypto_spot",
                    "trade_type": "explicit_fixture_type",
                    "last": 100.0,
                },
            ],
            [],
        )

        self.assertEqual("frontier_crypto_venue_map", rows[0]["trade_type"])
        self.assertNotIn("trade_type", rows[1])
        self.assertEqual("explicit_fixture_type", rows[2]["trade_type"])

    def test_untyped_crypto_spot_panic_program_emits_paper_long_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        logic = {
            "type": "observation_program",
            "universe": {
                "asset_classes": ["crypto_spot"],
                "market_types": ["spot"],
                "trade_types": ["frontier_crypto_venue_map"],
                "quotes": ["USDT"],
            },
            "calculated_features": {
                "shock_sigma": "-return_60m_bps / max(volatility_60m_bps, 10)",
                "recovery_slope": "return_1m_bps - return_5m_bps / 5",
                "cost_adjusted_snapback": "max(0, min(-return_15m_bps, 150) - 2 * spread_bps)",
            },
            "direction": "long",
            "entry_expression": (
                "microstructure_history_ready >= 1 and shock_sigma >= 1.5 "
                "and price_zscore_60m <= -1.25 and return_15m_bps <= -40 "
                "and return_60m_bps >= -500 and return_60m_bps <= -25 "
                "and return_1m_bps > 0 and return_5m_bps > 0 and recovery_slope > 0 "
                "and relative_volume_1m_60m >= 1.5 and spread_bps <= 8 "
                "and liquidity_score >= 0.65 and quality_score >= 60 and stale_minutes <= 2"
            ),
            "invalidation_expression": (
                "return_1m_bps <= -20 or spread_bps > 12 or return_60m_bps < -650 "
                "or price_zscore_60m > 0.5"
            ),
            "edge_expression": "cost_adjusted_snapback",
            "score_expression": "clip(40 + 10 * shock_sigma + recovery_slope - spread_bps, 0, 100)",
            "route_surface": "spot",
        }
        recommendation = {
            "recommendation_id": "rec_frontier_spot_input_contract",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "strategy_lab_experiment": {
                    "strategy_lab_id": "frontier_spot_input_contract",
                    "version": 1,
                    "experiment_type": "market_strategy",
                    "hypothesis": "Untyped raw crypto spot observations retain paper-only panic-snapback coverage.",
                    "source_surface": "spot",
                    "permitted_target_surface": ["spot"],
                    "strategy_logic": logic,
                    "data_requirements": {"paper_only": True},
                    "risk_gates": {"paper_only": True, "long_only": True},
                    "promotion_rules": {},
                },
            },
        }

        def spot_observation(price: float, observed_at: str, *, return_1m_bps: float = 0.0) -> dict:
            return {
                "venue": "OKX_SPOT",
                "inst_id": "OKX_SPOT:BTC-USDT",
                "market_type": "spot",
                "asset_class": "crypto_spot",
                "base": "BTC",
                "quote": "USDT",
                "last": price,
                "spread_bps": 2.0,
                "liquidity_score": 0.8,
                "quality_score": 80.0,
                "stale_minutes": 1.0,
                "microstructure_history_ready": 1.0,
                "relative_volume_1m_60m": 2.0,
                "return_1m_bps": return_1m_bps,
                "data_status": "reachable",
                "observed_at": observed_at,
                "price_source": "fixture",
            }

        history_prices = [100.0] * 8 + [99.5, 99.0, 97.0, 97.5]
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            for index, price in enumerate(history_prices):
                record_feature_snapshots(
                    conn,
                    [spot_observation(price, (now - dt.timedelta(minutes=60 - index * 5)).isoformat())],
                    cfg,
                )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [spot_observation(97.9, now.isoformat(), return_1m_bps=15.0)],
            )

        self.assertEqual(1, len(generated), report)
        self.assertTrue(generated[0]["paper_only"])
        self.assertEqual("frontier_crypto_venue_map", generated[0]["trade_type"])
        self.assertEqual("long_frontier_spot", generated[0]["direction"])
        self.assertEqual("spot", generated[0]["target_surface"])

    def test_sorted_shock_reversal_contract_activates_without_feature_extension(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        now -= dt.timedelta(minutes=now.minute % 5)
        logic = json.loads(json.dumps(shock_reversal_logic(), sort_keys=True))
        recommendation = lab_recommendation(
            "global_proxy_shock_reversal_observation_v1",
            logic,
        )
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            for index in range(12):
                record_feature_snapshots(
                    conn,
                    [
                        observation(
                            100.0 if index == 0 else 99.1,
                            (now - dt.timedelta(minutes=60 - index * 5)).isoformat(),
                        )
                    ],
                    cfg,
                )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                [observation(99.2, now.isoformat())],
            )
            row = conn.execute(
                """
                select status, compile_status, novelty_status, compile_diagnostics_json
                from strategy_lab_experiments where strategy_lab_id = ?
                """,
                ("global_proxy_shock_reversal_observation_v1",),
            ).fetchone()
            recommendations_table = conn.execute(
                """
                select count(*) from sqlite_master
                where type = 'table' and name = 'llm_recommendations'
                """
            ).fetchone()[0]
            feature_extensions = (
                conn.execute(
                    """
                    select count(*) from llm_recommendations
                    where recommendation_id like 'strategy_lab_feature_extension_%'
                    """
                ).fetchone()[0]
                if recommendations_table
                else 0
            )

        self.assertEqual(1, len(generated), report)
        self.assertEqual("global_proxy_shock_reversal", generated[0]["trade_type"])
        self.assertEqual("long_proxy", generated[0]["direction"])
        self.assertEqual("active_testing", row["status"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("novel", row["novelty_status"])
        self.assertEqual([], json.loads(row["compile_diagnostics_json"])["missing_features"])
        self.assertEqual(0, feature_extensions)

    def test_radar_runtime_selection_admits_observation_program_candidates(self) -> None:
        candidate = {
            "strategy_lab_id": "observation_runtime",
            "strategy_lab_logic_type": "observation_program",
            "venue": "YAHOO_PROXY",
            "inst_id": "TEST:ABC",
            "direction": "long_proxy",
            "trade_type": "global_market_discovery_proxy",
            "score": 70.0,
            "strategy_lab_surface_policy": {"eligible": True, "reason": "surface_compatible"},
        }
        selected, summary = _select_runtime_strategy_lab_candidates([candidate], settings())
        self.assertEqual([candidate], selected)
        self.assertEqual(1, summary["selected_count"])

    def test_missing_feature_creates_code_evolution_recommendation(self) -> None:
        logic = program_logic()
        logic["calculated_features"] = {"surprise": "sentiment_surprise * return_5m_bps"}
        logic["entry_expression"] = "surprise > 5"
        logic["long_expression"] = "surprise > 0"
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(
                conn,
                lab_recommendation("needs_sentiment_feature", logic),
            )
            generate_strategy_lab_candidates(
                conn,
                settings(),
                [],
                {"TEST:ABC": observation(101.0, dt.datetime.now(dt.timezone.utc).isoformat())},
            )
            experiment = conn.execute(
                "select status, compile_status, compile_diagnostics_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("needs_sentiment_feature",),
            ).fetchone()
            rec = conn.execute(
                "select action, payload_json from llm_recommendations where recommendation_id like 'strategy_lab_feature_extension_%'"
            ).fetchone()
        self.assertEqual("needs_data", experiment["status"])
        self.assertEqual("needs_data", experiment["compile_status"])
        self.assertEqual("propose_code_change", rec["action"])
        self.assertIn("sentiment_surprise", json.loads(rec["payload_json"])["evidence"]["missing_features"])

    def test_canonical_signature_deduplicates_equivalent_programs(self) -> None:
        first = program_logic()
        second = copy.deepcopy(first)
        second["universe"] = {"asset_classes": ["equity"], "venues": ["yahoo_proxy"]}
        self.assertEqual(novelty_signature(first), novelty_signature(second))
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_recommendation("novel_first", first))
            ingest_strategy_lab_recommendation(conn, lab_recommendation("duplicate_second", second))
            row = conn.execute(
                "select status, novelty_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("duplicate_second",),
            ).fetchone()
        self.assertEqual("rejected_invalid", row["status"])
        self.assertEqual("duplicate_experiment", row["novelty_status"])

    def test_observation_promotion_targets_generated_plugin_and_parity_test(self) -> None:
        experiment = {
            "strategy_lab_id": "observation_momentum_v1",
            "version": 1,
            "experiment_type": "market_strategy",
            "hypothesis": "Fresh momentum continues after costs.",
            "strategy_logic": program_logic(),
            "risk_gates": {},
            "novelty_signature": novelty_signature(program_logic()),
        }
        with memory_db() as conn:
            rec_id = _queue_promotion(conn, experiment, {"metrics": {"count": 30}}, {})
            payload = json.loads(
                conn.execute(
                    "select payload_json from llm_recommendations where recommendation_id = ?",
                    (rec_id,),
                ).fetchone()["payload_json"]
            )
            self.assertTrue(payload["paper_testable_surface"].startswith("paper:strategy_lab:"))
            self.assertTrue(payload["behavioral_gate"])
            self.assertTrue(payload["rollback_criteria"])
            self.assertIn("quality_evidence", payload["evidence"])
        files = payload["code_change"]["expected_files"]
        self.assertIn("src/signals/generated/observation_momentum_v1.py", files)
        self.assertIn("tests/test_generated_strategy_parity.py", files)
        self.assertIn("reproduce", payload["proposed_change"]["promotion_target"]["parity_requirement"])

    def test_plugin_parity_helper_compares_interpreter_candidates(self) -> None:
        cfg = settings()
        experiment = {
            "strategy_lab_id": "parity_lab",
            "version": 1,
            "hypothesis": "Quality momentum",
            "strategy_logic": {
                **program_logic(),
                "entry_expression": "quality_score >= 60",
                "long_expression": "True",
                "short_expression": "False",
                "edge_expression": "10",
            },
        }
        frames = [observation(101.0, dt.datetime.now(dt.timezone.utc).isoformat())]

        class Plugin:
            @staticmethod
            def generate(_observations, context=None):
                candidates, _ = generate_program_candidates(
                    context["strategy_lab_experiment"],
                    context["feature_frames"],
                    context["settings"],
                )
                return candidates

        assert_plugin_parity(Plugin, experiment, frames, cfg)


if __name__ == "__main__":
    unittest.main()
