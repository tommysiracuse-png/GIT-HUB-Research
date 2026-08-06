"""Observation-native, paper-only Strategy Lab programs.

The model supplies declarative expressions. This module owns validation,
feature history, deterministic evaluation, and conversion to the ordinary
candidate contract. It never evaluates Python source from a model.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from typing import Any, Iterable


LOGIC_TYPE = "observation_program"
OUTPUT_TRADE_TYPE_SURFACES = {
    "global_proxy_shock_reversal": "proxy",
    "perp_funding_capture": "perp",
}
OUTPUT_TRADE_TYPE_TARGET_SURFACES = {
    "global_proxy_shock_reversal": "proxy",
    "perp_funding_capture": "perp_funding_basis",
}
NAV_REFERENCE_ROUTE_SURFACE = "nav_reference"
NAV_REFERENCE_QUALITY_STATUS = "official_month_end_nav"
NAV_REFERENCE_REJECT_REASON = "factsheet_nav_not_entry_quality_quote"
AUCTION_REFERENCE_ROUTE_SURFACE = "auction_reference"
AUCTION_REFERENCE_QUALITY_STATUS = "official_auction_result"
AUCTION_REFERENCE_REJECT_REASON = "official_auction_result_not_executable_quote"
ALLOWANCE_AUCTION_REFERENCE_REJECT_REASON = "official_allowance_auction_reference_not_order_routable"
SHOCK_REVERSAL_CALCULATED_FEATURES = {
    "shock_magnitude_bps": "abs(return_60m_bps)",
    "shock_sigma": "abs(return_60m_bps) / max(volatility_60m_bps, 10)",
    "flip_strength_bps": "max(0, -(return_5m_bps * return_60m_bps) / max(abs(return_60m_bps), 1))",
}
SHOCK_REVERSAL_DIRECTION_EXPRESSIONS = {
    "long_expression": "return_60m_bps < 0 and return_5m_bps > 0",
    "short_expression": "return_60m_bps > 0 and return_5m_bps < 0",
}
SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": math.sqrt,
    "log": math.log,
    "log1p": math.log1p,
    "clip": lambda value, low, high: max(low, min(high, value)),
}
BASE_FEATURES = {
    "last",
    "spread_bps",
    "liquidity_score",
    "quality_score",
    "funding_bps",
    "funding_history_count",
    "funding_history_avg_bps",
    "funding_history_last_bps",
    "time_to_next_funding_minutes",
    "basis_bps",
    "basis_observed",
    "basis_zscore_60m",
    "basis_volatility_60m_bps",
    "basis_change_5m_bps",
    "basis_history_ready",
    "net_carry_edge_bps",
    "round_trip_cost_bps",
    "dislocation_bps",
    "cross_venue_dislocation_bps",
    "stale_minutes",
    "change_24h_pct",
    "return_1m_bps",
    "return_5m_bps",
    "return_15m_bps",
    "return_60m_bps",
    "return_4h_bps",
    "return_1d_bps",
    "momentum_15m_bps",
    "momentum_60m_bps",
    "momentum_4h_bps",
    "volatility_60m_bps",
    "volatility_4h_bps",
    "price_zscore_60m",
    "price_zscore_4h",
    "relative_strength_60m_bps",
    "relative_strength_4h_bps",
    "quote_volume_1m",
    "relative_volume_1m_60m",
    "microstructure_history_ready",
    "rolling_vwap_60m",
    "vwap_dislocation_bps",
    "price_above_rolling_vwap",
    "new_high_60m",
    "momentum_confirmation_count",
    "momentum_confirmation_ratio",
    "rolling_24_hour_volume",
    "listing_age_days",
    "cross_venue_reference_price",
    "cross_venue_dislocation_bps",
    # Auction fields are normalized, paper-only observation features.  They
    # deliberately describe the public result rather than an executable quote.
    "auction_coverage_ratio",
    "auction_tail_bps",
    "auction_term_days",
    "auction_average_yield_pct",
    "auction_stop_out_yield_pct",
    "auction_result_published",
    "average_interest_rate_pct",
    "previous_average_interest_rate_pct",
    "average_price_per_100",
    "lowest_accepted_price_per_100",
    "oversubscription_pct",
    "maturity_days",
    "auction_settlement_price_usd",
    "allowances_offered",
    "allowances_sold",
    "allowance_sellthrough_ratio",
    "paired_current_advance_observed",
    "current_price_usd_by_auction",
    "advance_price_usd_by_auction",
    "current_sellthrough",
    "advance_sellthrough",
    "term_discount_bps",
    "term_discount_zscore",
    # EEX publishes completed secondary spot trades rather than executable
    # quotes. Keep the disclosed values distinct from the generic `last`
    # feature so contracts can require the exact reported-trade provenance.
    "reported_trade_price",
    "reported_trade_volume",
    "reported_trade_valid",
    "cpotr_price_card_pair_observed",
    "suggested_opening_price",
    "previous_settlement_price",
    "cpotr_opening_gap_bps",
    "available_exploratory_blocks",
    "new_exploratory_blocks",
    "offshore_new_blocks",
    "onshore_new_blocks",
    "exchange_established_year",
    "cpotr_launch_year",
    "gofx_launch_year",
    "gofx_micro_launch_year",
    "crude_oil_contract_launch_year",
    "exchange_age_years",
    "years_since_cpotr_launch",
    "years_since_gofx_launch",
    "years_since_gofx_micro_launch",
    "years_since_crude_oil_contract_launch",
}
PERP_FUNDING_CAPTURE_REQUIRED_FEATURES = {
    "funding_bps",
    "funding_history_count",
    "funding_history_avg_bps",
    "funding_history_last_bps",
    "basis_history_ready",
    "basis_zscore_60m",
    "basis_volatility_60m_bps",
    "basis_change_5m_bps",
    "net_carry_edge_bps",
}
PROGRAM_CANDIDATE_PASSTHROUGH_FIELDS = {
    "hedge_venue",
    "route_hedge_venue",
    "hedge_instrument",
    "hedge_instrument_id",
    "hedge_symbol",
    "route_hedge_instrument_id",
    "route_hedge_symbol",
    "fee_model",
    "fee_model_status",
    "fees_modeled",
    "total_fee_bps",
    "estimated_fee_bps",
    "fee_bps",
    "route_fee_bps",
    "estimated_round_trip_cost_bps",
    "round_trip_cost_bps",
    "total_cost_bps",
    "paper_leg_mapping_valid",
    "leg_mapping_paper_valid",
    "paper_valid_leg_mapping",
    "paper_leg_mapping",
    "leg_mapping",
    "requires_hedge",
    "hedge_required",
    "transfer_required",
    "requires_transfer",
    "cross_venue_transfer_required",
    "hedge_mode_required",
    "execution_route",
    "execution_feasibility",
    "venue_capabilities",
    "route_requirements",
    "route_id",
    "route_status",
    "market_surface",
    "freshness_state",
    "candidate_reject_reason",
    "price_basis",
    "price_type",
    "contract_month",
    "price_reference_role",
    "price_source",
    "source_adapter_id",
    "source_url",
    "source_programme_url",
    "source_contract_url",
    "source_record_type",
    "proxy_quality_status",
    "proxy_symbol",
    "companion_quote_symbol",
    "available_exploratory_blocks",
    "new_exploratory_blocks",
    "offshore_new_blocks",
    "onshore_new_blocks",
    "trade_id",
    "traded_volume",
    "allowance_category",
    "auction_number",
    "event_date",
    "isin",
    "maturity_date_iso",
    "source_quality_status",
    "reserve_sale",
    "price_available",
    "auction_settlement_price_usd",
    "allowances_offered",
    "allowances_sold",
    "reported_trade_price",
    "reported_trade_volume",
    "reported_trade_valid",
}
METADATA_NAMES = {
    "venue",
    "inst_id",
    "trade_type",
    "asset_class",
    "region",
    "market_type",
    "quote",
    "base",
    "session_status",
    "route_status",
    "quality_status",
    "data_status",
    "market_surface",
    "freshness_state",
    "candidate_reject_reason",
    "price_basis",
    "price_type",
    "contract_month",
    "price_reference_role",
    "price_source",
    "allowance_category",
    "auction_number",
    "event_date",
    "reserve_sale",
    "price_available",
}
ALLOWED_AST_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
    ast.Call,
)


class ProgramValidationError(ValueError):
    """A program expression is unsafe or malformed."""


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _parse_time(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _bucket_time(value: Any, minutes: int) -> str:
    parsed = _parse_time(value)
    minute = parsed.minute - (parsed.minute % max(1, minutes))
    return parsed.replace(minute=minute, second=0, microsecond=0).isoformat()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _observation_rows(observations: dict[str, dict] | Iterable[dict] | None) -> list[dict]:
    raw_rows = observations.values() if isinstance(observations, dict) else (observations or [])
    rows: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
        row = {**candidate, **{key: value for key, value in raw.items() if key != "candidate"}}
        inst_id = str(row.get("inst_id") or row.get("instrument_id") or "").strip()
        venue = str(row.get("venue") or candidate.get("venue") or "UNKNOWN").strip()
        last = _float(row.get("last", row.get("price")), math.nan)
        if not inst_id or not math.isfinite(last) or last <= 0:
            continue
        row["inst_id"] = inst_id
        row["venue"] = venue
        row["last"] = last
        row["observed_at"] = str(
            row.get("observed_at") or row.get("seen_at") or dt.datetime.now(dt.timezone.utc).isoformat()
        )
        row["price_source"] = str(row.get("price_source") or venue or "scanner")
        rows.append(row)
    return rows


def _instrument_key(row: dict) -> tuple[str, str]:
    return str(row.get("venue") or "UNKNOWN"), str(row.get("inst_id") or "")


def _load_history(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    cutoff: str,
    max_points: int,
) -> dict[tuple[str, str], list[dict]]:
    history: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for start in range(0, len(keys), 300):
        chunk = keys[start : start + 300]
        clauses = " or ".join("(venue = ? and inst_id = ?)" for _ in chunk)
        params: list[Any] = [cutoff]
        for venue, inst_id in chunk:
            params.extend([venue, inst_id])
        rows = conn.execute(
            f"""
            select bucket_at, observed_at, venue, inst_id, trade_type, last, price_source, features_json
            from strategy_feature_snapshots
            where bucket_at >= ? and ({clauses})
            order by venue, inst_id, bucket_at
            """,
            params,
        ).fetchall()
        for raw in rows:
            item = dict(raw)
            item["features"] = _json(item.pop("features_json"), {})
            history[(str(item["venue"]), str(item["inst_id"]))].append(item)
    if max_points > 0:
        history = {key: rows[-max_points:] for key, rows in history.items()}
    return history


def _return_bps(current: float, history: list[dict], periods: int) -> float:
    if len(history) < periods:
        return 0.0
    prior = _float(history[-periods].get("last"), 0.0)
    return ((current / prior) - 1.0) * 10_000.0 if prior > 0 else 0.0


def _volatility_bps(prices: list[float]) -> float:
    returns = [((right / left) - 1.0) * 10_000.0 for left, right in zip(prices, prices[1:]) if left > 0]
    return statistics.pstdev(returns) if len(returns) >= 2 else 0.0


def _zscore(current: float, prices: list[float]) -> float:
    if len(prices) < 3:
        return 0.0
    deviation = statistics.pstdev(prices)
    return (current - statistics.fmean(prices)) / deviation if deviation > 0 else 0.0


def _stored_feature(item: dict, name: str) -> Any:
    features = item.get("features")
    return features.get(name) if isinstance(features, dict) else None


def _ratio(numerator: Any, denominator: Any) -> float:
    numerator_value = _float(numerator)
    denominator_value = _float(denominator)
    if denominator_value <= 0:
        return 0.0
    return numerator_value / denominator_value


def _series_zscore(current: float, history: list[float]) -> float:
    values = [float(item) for item in history if math.isfinite(float(item))]
    if len(values) < 3:
        return 0.0
    deviation = statistics.pstdev(values)
    return (current - statistics.fmean(values)) / deviation if deviation > 0 else 0.0


def _allowance_auction_group_key(frame: dict) -> tuple[str, str, str]:
    auction_number = str(frame.get("auction_number") or "").strip()
    event_date = str(frame.get("event_date") or "").strip()
    fallback = event_date or str(frame.get("inst_id") or "").strip()
    return (
        str(frame.get("venue") or ""),
        str(frame.get("market_surface") or ""),
        auction_number or fallback,
    )


def _load_allowance_auction_discount_history(
    conn: sqlite3.Connection,
    frames: list[dict],
    cutoff: str,
    max_points: int,
) -> dict[tuple[str, str], list[float]]:
    pairs = {
        (str(frame.get("venue") or ""), str(frame.get("market_surface") or ""))
        for frame in frames
        if str(frame.get("allowance_category") or "").strip()
    }
    history: dict[tuple[str, str], list[float]] = defaultdict(list)
    for venue, surface in sorted(pairs):
        if not venue or not surface:
            continue
        rows = conn.execute(
            """
            select features_json
            from strategy_feature_snapshots
            where bucket_at >= ? and venue = ?
            order by bucket_at
            """,
            (cutoff, venue),
        ).fetchall()
        values: list[float] = []
        for raw in rows:
            features = _json(raw["features_json"], {})
            if str(features.get("market_surface") or "") != surface:
                continue
            if str(features.get("allowance_category") or "") != "current":
                continue
            if _float(features.get("paired_current_advance_observed")) < 1.0:
                continue
            value = _float(features.get("term_discount_bps"))
            if math.isfinite(value):
                values.append(value)
        history[(venue, surface)] = values[-max_points:] if max_points > 0 else values
    return history


def _annotate_allowance_auction_frames(
    frames: list[dict],
    discount_history: dict[tuple[str, str], list[float]],
) -> None:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for frame in frames:
        if not str(frame.get("allowance_category") or "").strip():
            continue
        groups[_allowance_auction_group_key(frame)].append(frame)
    for group in groups.values():
        by_category = {
            str(item.get("allowance_category") or "").strip().lower(): item
            for item in group
        }
        current = by_category.get("current")
        advance = by_category.get("advance")
        current_price = _float(
            (current or {}).get("auction_settlement_price_usd", (current or {}).get("last"))
        )
        advance_price = _float(
            (advance or {}).get("auction_settlement_price_usd", (advance or {}).get("last"))
        )
        current_sellthrough = _ratio(
            (current or {}).get("allowances_sold"),
            (current or {}).get("allowances_offered"),
        )
        advance_sellthrough = _ratio(
            (advance or {}).get("allowances_sold"),
            (advance or {}).get("allowances_offered"),
        )
        pair_present = current is not None and advance is not None and current_price > 0 and advance_price > 0
        term_discount_bps = (
            ((current_price - advance_price) / current_price) * 10_000.0
            if pair_present
            else 0.0
        )
        history_key = (
            str((current or group[0]).get("venue") or ""),
            str((current or group[0]).get("market_surface") or ""),
        )
        term_discount_zscore = _series_zscore(
            term_discount_bps,
            discount_history.get(history_key, []),
        ) if pair_present else 0.0
        for frame in group:
            frame["auction_settlement_price_usd"] = _float(
                frame.get("auction_settlement_price_usd", frame.get("last"))
            )
            frame["allowances_offered"] = max(0.0, _float(frame.get("allowances_offered")))
            frame["allowances_sold"] = max(0.0, _float(frame.get("allowances_sold")))
            frame["allowance_sellthrough_ratio"] = _ratio(
                frame.get("allowances_sold"),
                frame.get("allowances_offered"),
            )
            frame["paired_current_advance_observed"] = 1.0 if pair_present else 0.0
            frame["current_price_usd_by_auction"] = current_price if pair_present else 0.0
            frame["advance_price_usd_by_auction"] = advance_price if pair_present else 0.0
            frame["current_sellthrough"] = current_sellthrough if pair_present else 0.0
            frame["advance_sellthrough"] = advance_sellthrough if pair_present else 0.0
            frame["term_discount_bps"] = term_discount_bps if pair_present else 0.0
            frame["term_discount_zscore"] = term_discount_zscore if pair_present else 0.0


def _icdx_cpotr_price_card_group_key(frame: dict) -> tuple[str, str, str]:
    contract_month = str(frame.get("contract_month") or "").strip()
    fallback = contract_month or str(frame.get("inst_id") or "").strip()
    return (
        str(frame.get("venue") or ""),
        str(frame.get("market_surface") or ""),
        fallback,
    )


def _annotate_icdx_cpotr_price_card_frames(frames: list[dict]) -> None:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for frame in frames:
        if str(frame.get("venue") or "").upper() != "ICDX":
            continue
        if str(frame.get("market_surface") or "") != "icdx_cpotr":
            continue
        if str(frame.get("trade_type") or "") != "official_price_card_reference":
            continue
        groups[_icdx_cpotr_price_card_group_key(frame)].append(frame)
    for group in groups.values():
        suggested = next(
            (frame for frame in group if str(frame.get("price_type") or "") == "suggested_opening"),
            None,
        )
        settlement = next(
            (frame for frame in group if str(frame.get("price_type") or "") == "previous_settlement"),
            None,
        )
        suggested_price = max(0.0, _float((suggested or {}).get("last")))
        settlement_price = max(0.0, _float((settlement or {}).get("last")))
        pair_present = bool(suggested and settlement and suggested_price > 0 and settlement_price > 0)
        opening_gap_bps = (
            ((suggested_price - settlement_price) / settlement_price) * 10_000.0
            if pair_present
            else 0.0
        )
        for frame in group:
            frame["suggested_opening_price"] = suggested_price
            frame["previous_settlement_price"] = settlement_price
            frame["cpotr_price_card_pair_observed"] = 1.0 if pair_present else 0.0
            frame["cpotr_opening_gap_bps"] = opening_gap_bps if pair_present else 0.0


def _base_symbol(row: dict) -> str:
    base = str(row.get("base") or "").upper()
    if base:
        return base
    symbol = str(row.get("symbol") or row.get("inst_id") or "").upper().split(":")[-1]
    for separator in ("-", "_", "/"):
        if separator in symbol:
            return symbol.split(separator)[0]
    return symbol


def _feature_frame(row: dict, history: list[dict], peer_prices: list[float]) -> dict:
    last = _float(row.get("last"))
    prices = [_float(item.get("last")) for item in history if _float(item.get("last")) > 0]
    recent_60 = (prices + [last])[-13:]
    recent_4h = (prices + [last])[-49:]
    peer_median = statistics.median(peer_prices) if peer_prices else last
    dislocation = ((last / peer_median) - 1.0) * 10_000.0 if peer_median > 0 else 0.0
    return_15m = _return_bps(last, history, 3)
    return_60m = _return_bps(last, history, 12)
    return_4h = _return_bps(last, history, 48)
    return_1d = _return_bps(last, history, 288)
    basis_present = row.get("basis_bps") not in (None, "")
    basis = _float(row.get("basis_bps")) if basis_present else 0.0
    recent_basis_history = history[-12:]
    historical_basis = [
        _float(_stored_feature(item, "basis_bps"))
        for item in recent_basis_history
        if _float(_stored_feature(item, "basis_observed")) >= 1.0
    ]
    basis_history_ready = bool(
        basis_present
        and len(recent_basis_history) == 12
        and len(historical_basis) == len(recent_basis_history)
    )
    recent_basis = historical_basis + [basis] if basis_history_ready else []
    basis_change_5m = basis - historical_basis[-1] if basis_history_ready else 0.0
    average_interest_rate_pct = max(0.0, _float(row.get("average_interest_rate_pct")))
    previous_average_interest_rate_pct = max(
        0.0,
        _float(row.get("previous_average_interest_rate_pct")),
    )
    average_price_per_100 = max(0.0, _float(row.get("average_price_per_100"), last))
    lowest_accepted_price_per_100 = max(
        0.0,
        _float(row.get("lowest_accepted_price_per_100")),
    )
    oversubscription_pct = max(0.0, _float(row.get("oversubscription_pct")))
    maturity_days = max(0.0, _float(row.get("maturity_days")))
    auction_at = str(
        row.get("auction_at")
        or row.get("result_published_date")
        or row.get("issue_date")
        or row.get("observed_at")
        or ""
    )
    coverage_ratio = _float(row.get("coverage_ratio"))
    if coverage_ratio <= 0 and oversubscription_pct > 0:
        coverage_ratio = 1.0 + (oversubscription_pct / 100.0)
    return {
        **row,
        "last": last,
        "spread_bps": _float(row.get("spread_bps"), 0.0),
        "liquidity_score": _float(row.get("liquidity_score"), 0.5),
        "quality_score": _float(row.get("quality_score"), 50.0),
        "funding_bps": _float(row.get("funding_bps")),
        "funding_history_count": max(0.0, _float(row.get("funding_history_count"))),
        "funding_history_avg_bps": _float(row.get("funding_history_avg_bps")),
        "funding_history_last_bps": _float(row.get("funding_history_last_bps")),
        "time_to_next_funding_minutes": max(
            0.0,
            _float(row.get("time_to_next_funding_minutes")),
        ),
        "basis_bps": basis,
        "basis_observed": 1.0 if basis_present else 0.0,
        "basis_zscore_60m": _zscore(basis, recent_basis) if basis_history_ready else 0.0,
        "basis_volatility_60m_bps": (
            statistics.pstdev(recent_basis) if basis_history_ready else 0.0
        ),
        "basis_change_5m_bps": basis_change_5m,
        "basis_history_ready": 1.0 if basis_history_ready else 0.0,
        "net_carry_edge_bps": _float(row.get("net_carry_edge_bps")),
        "round_trip_cost_bps": max(
            0.0,
            _float(
                row.get(
                    "round_trip_cost_bps",
                    row.get("estimated_round_trip_cost_bps"),
                )
            ),
        ),
        "dislocation_bps": _float(row.get("dislocation_bps", row.get("edge_bps_estimate"))),
        "cross_venue_dislocation_bps": dislocation,
        "stale_minutes": _float(
            row.get("stale_minutes"),
            _float(row.get("freshness_age_seconds")) / 60.0,
        ),
        "change_24h_pct": _float(row.get("change_24h_pct"), return_1d / 100.0),
        "return_1m_bps": _float(row.get("return_1m_bps")),
        "return_5m_bps": _return_bps(last, history, 1),
        "return_15m_bps": return_15m,
        "return_60m_bps": return_60m,
        "return_4h_bps": return_4h,
        "return_1d_bps": return_1d,
        "momentum_15m_bps": return_15m,
        "momentum_60m_bps": return_60m,
        "momentum_4h_bps": return_4h,
        "volatility_60m_bps": _volatility_bps(recent_60),
        "volatility_4h_bps": _volatility_bps(recent_4h),
        "price_zscore_60m": _zscore(last, recent_60),
        "price_zscore_4h": _zscore(last, recent_4h),
        "relative_strength_60m_bps": return_60m,
        "relative_strength_4h_bps": return_4h,
        "quote_volume_1m": max(0.0, _float(row.get("quote_volume_1m"))),
        "relative_volume_1m_60m": max(0.0, _float(row.get("relative_volume_1m_60m"))),
        "microstructure_history_ready": 1.0 if _float(row.get("microstructure_history_ready")) >= 1.0 else 0.0,
        "rolling_vwap_60m": _float(row.get("rolling_vwap_60m"), last),
        "vwap_dislocation_bps": _float(row.get("vwap_dislocation_bps")),
        "price_above_rolling_vwap": 1.0 if _float(row.get("price_above_rolling_vwap")) >= 1.0 else 0.0,
        "new_high_60m": 1.0 if _float(row.get("new_high_60m")) >= 1.0 else 0.0,
        "momentum_confirmation_count": max(0.0, _float(row.get("momentum_confirmation_count"))),
        "momentum_confirmation_ratio": max(0.0, _float(row.get("momentum_confirmation_ratio"))),
        "rolling_24_hour_volume": max(0.0, _float(row.get("rolling_24_hour_volume"), _float(row.get("quote_volume_24h")))),
        "listing_age_days": max(0.0, _float(row.get("listing_age_days"))),
        "cross_venue_reference_price": _float(row.get("cross_venue_reference_price")),
        "cross_venue_dislocation_bps": _float(
            row.get("cross_venue_dislocation_bps", row.get("venue_deviation_bps"))
        ),
        "auction_coverage_ratio": max(0.0, coverage_ratio),
        "auction_tail_bps": _float(row.get("tail_bps")),
        "auction_term_days": max(0.0, _float(row.get("term_days"), maturity_days)),
        "auction_average_yield_pct": max(
            0.0,
            _float(row.get("average_yield_pct"), average_interest_rate_pct),
        ),
        "auction_stop_out_yield_pct": max(0.0, _float(row.get("stop_out_yield_pct"))),
        "auction_result_published": (
            1.0 if str(row.get("quality_status") or "") == AUCTION_REFERENCE_QUALITY_STATUS else 0.0
        ),
        "average_interest_rate_pct": average_interest_rate_pct,
        "previous_average_interest_rate_pct": previous_average_interest_rate_pct,
        "average_price_per_100": average_price_per_100,
        "lowest_accepted_price_per_100": lowest_accepted_price_per_100,
        "oversubscription_pct": oversubscription_pct,
        "maturity_days": maturity_days,
        "auction_at": auction_at,
        "auction_settlement_price_usd": _float(
            row.get("auction_settlement_price_usd"),
            _float(row.get("last")),
        ),
        "allowances_offered": max(0.0, _float(row.get("allowances_offered"))),
        "allowances_sold": max(0.0, _float(row.get("allowances_sold"))),
        "allowance_sellthrough_ratio": _ratio(row.get("allowances_sold"), row.get("allowances_offered")),
        "paired_current_advance_observed": max(0.0, _float(row.get("paired_current_advance_observed"))),
        "current_price_usd_by_auction": max(0.0, _float(row.get("current_price_usd_by_auction"))),
        "advance_price_usd_by_auction": max(0.0, _float(row.get("advance_price_usd_by_auction"))),
        "current_sellthrough": max(0.0, _float(row.get("current_sellthrough"))),
        "advance_sellthrough": max(0.0, _float(row.get("advance_sellthrough"))),
        "term_discount_bps": _float(row.get("term_discount_bps")),
        "term_discount_zscore": _float(row.get("term_discount_zscore")),
        "reported_trade_price": _float(row.get("reported_trade_price", last)),
        "reported_trade_volume": max(
            0.0,
            _float(row.get("reported_trade_volume", row.get("traded_volume"))),
        ),
        "reported_trade_valid": max(0.0, _float(row.get("reported_trade_valid"))),
    }


def build_feature_frames(
    conn: sqlite3.Connection,
    observations: dict[str, dict] | Iterable[dict] | None,
    settings: dict,
) -> list[dict]:
    rows = _observation_rows(observations)
    if not rows:
        return []
    cfg = settings.get("strategy_lab", {})
    retention_days = max(1, int(cfg.get("feature_snapshot_retention_days", 14)))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).isoformat()
    history = _load_history(
        conn,
        list(dict.fromkeys(_instrument_key(row) for row in rows)),
        cutoff,
        int(cfg.get("feature_history_max_points", 4032)),
    )
    snapshot_minutes = max(1, int(cfg.get("feature_snapshot_minutes", 5)))
    peers: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        peers[_base_symbol(row)].append(_float(row.get("last")))
    frames = [
        _feature_frame(
            row,
            [
                item
                for item in history.get(_instrument_key(row), [])
                if str(item.get("bucket_at") or "") < _bucket_time(row.get("observed_at"), snapshot_minutes)
            ],
            peers.get(_base_symbol(row), []),
        )
        for row in rows
    ]
    allowance_discount_history = _load_allowance_auction_discount_history(
        conn,
        frames,
        cutoff,
        int(cfg.get("feature_history_max_points", 4032)),
    )
    _annotate_allowance_auction_frames(frames, allowance_discount_history)
    _annotate_icdx_cpotr_price_card_frames(frames)
    by_inst = {str(frame["inst_id"]): frame for frame in frames}
    for frame in frames:
        benchmark_id = str(frame.get("benchmark_inst_id") or "")
        benchmark = by_inst.get(benchmark_id)
        if benchmark:
            frame["relative_strength_60m_bps"] -= _float(benchmark.get("return_60m_bps"))
            frame["relative_strength_4h_bps"] -= _float(benchmark.get("return_4h_bps"))
    return frames


def record_feature_snapshots(
    conn: sqlite3.Connection,
    observations: dict[str, dict] | Iterable[dict] | None,
    settings: dict,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    frames = build_feature_frames(conn, observations, settings)
    snapshot_minutes = max(1, int(cfg.get("feature_snapshot_minutes", 5)))
    inserted = 0
    for frame in frames:
        bucket_at = _bucket_time(frame.get("observed_at"), snapshot_minutes)
        compact = {key: frame.get(key) for key in sorted(BASE_FEATURES | METADATA_NAMES) if frame.get(key) is not None}
        before = conn.total_changes
        conn.execute(
            """
            insert into strategy_feature_snapshots (
                bucket_at, observed_at, venue, inst_id, trade_type, last, price_source, features_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(bucket_at, venue, inst_id) do update set
                observed_at = excluded.observed_at,
                trade_type = excluded.trade_type,
                last = excluded.last,
                price_source = excluded.price_source,
                features_json = excluded.features_json
            """,
            (
                bucket_at,
                str(frame.get("observed_at")),
                str(frame.get("venue") or "UNKNOWN"),
                str(frame.get("inst_id")),
                str(frame.get("trade_type") or "unknown"),
                float(frame["last"]),
                str(frame.get("price_source") or "scanner"),
                json.dumps(compact, sort_keys=True),
            ),
        )
        inserted += int(conn.total_changes > before)
    retention_days = max(1, int(cfg.get("feature_snapshot_retention_days", 14)))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).isoformat()
    expired = conn.execute("delete from strategy_feature_snapshots where bucket_at < ?", (cutoff,)).rowcount
    max_rows = max(1, int(cfg.get("feature_snapshot_max_rows", 2_000_000)))
    row_count = int(conn.execute("select count(*) from strategy_feature_snapshots").fetchone()[0])
    overflow = max(0, row_count - max_rows)
    if overflow:
        conn.execute(
            """
            delete from strategy_feature_snapshots
            where id in (select id from strategy_feature_snapshots order by bucket_at, id limit ?)
            """,
            (overflow,),
        )
    conn.commit()
    return frames, {
        "snapshot_minutes": snapshot_minutes,
        "feature_frames": len(frames),
        "rows_written": inserted,
        "rows_expired": max(0, int(expired)),
        "rows_pruned_for_cap": overflow,
        "stored_rows": row_count - overflow,
        "retention_days": retention_days,
        "max_rows": max_rows,
    }


def _parse_expression(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise ProgramValidationError("expression_required")
    if len(expression) > 1000:
        raise ProgramValidationError("expression_too_long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ProgramValidationError(f"invalid_expression:{exc.msg}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 120:
        raise ProgramValidationError("expression_too_complex")
    for node in nodes:
        if not isinstance(node, ALLOWED_AST_NODES):
            raise ProgramValidationError(f"unsafe_expression_node:{type(node).__name__}")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if not math.isfinite(float(node.value)) or abs(float(node.value)) > 1_000_000_000_000:
                raise ProgramValidationError("numeric_constant_out_of_bounds")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, (int, float)):
                raise ProgramValidationError("power_exponent_must_be_constant")
            if abs(float(node.right.value)) > 8:
                raise ProgramValidationError("power_exponent_out_of_bounds")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCTIONS or node.keywords:
                raise ProgramValidationError("unsafe_function_call")
    return tree


def expression_names(expression: str) -> set[str]:
    tree = _parse_expression(expression)
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in SAFE_FUNCTIONS
    }


def evaluate_expression(expression: str, values: dict[str, Any]) -> Any:
    tree = _parse_expression(expression)
    missing = sorted(expression_names(expression) - set(values))
    if missing:
        raise ProgramValidationError("missing_features:" + ",".join(missing))
    return eval(compile(tree, "<strategy-program>", "eval"), {"__builtins__": {}, **SAFE_FUNCTIONS}, dict(values))


def _canonical_expression(expression: Any) -> str:
    if not isinstance(expression, str) or not expression.strip():
        return ""
    return ast.dump(_parse_expression(expression), annotate_fields=True, include_attributes=False)


def canonical_program(logic: dict) -> dict:
    universe = logic.get("universe") if isinstance(logic.get("universe"), dict) else {}
    canonical_universe = {
        key: sorted({str(item).strip().upper() for item in value if str(item).strip()})
        if isinstance(value, list)
        else str(value).strip().upper()
        for key, value in sorted(universe.items())
        if value not in (None, "", [])
    }
    calculated = logic.get("calculated_features") if isinstance(logic.get("calculated_features"), dict) else {}
    return {
        "type": LOGIC_TYPE,
        "universe": canonical_universe,
        "calculated_features": {key: _canonical_expression(value) for key, value in sorted(calculated.items())},
        "entry_expression": _canonical_expression(logic.get("entry_expression") or "True"),
        "invalidation_expression": _canonical_expression(logic.get("invalidation_expression") or "False"),
        "long_expression": _canonical_expression(logic.get("long_expression") or "False"),
        "short_expression": _canonical_expression(logic.get("short_expression") or "False"),
        "direction": str(logic.get("direction") or "").lower(),
        "edge_expression": _canonical_expression(logic.get("edge_expression") or "0"),
        "score_expression": _canonical_expression(logic.get("score_expression") or "50"),
        "route_surface": str(logic.get("route_surface") or "auto").lower(),
        "output_trade_type": str(logic.get("output_trade_type") or "").lower(),
    }


def novelty_signature(logic: dict) -> str:
    payload = json.dumps(canonical_program(logic), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_output_trade_type_contract(program: dict) -> None:
    output_trade_type = program.get("output_trade_type")
    if output_trade_type == "perp_funding_capture":
        universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
        trade_types = universe.get("trade_types")
        trade_type_values = trade_types if isinstance(trade_types, list) else [trade_types] if trade_types else []
        if {str(value).strip().lower() for value in trade_type_values} != {"perp_funding_basis"}:
            raise ProgramValidationError("perp_funding_capture_requires_funding_basis_universe")
        if universe.get("inst_ids"):
            raise ProgramValidationError("perp_funding_capture_must_not_pin_instruments")
        if str(program.get("direction") or "").lower() != "short":
            raise ProgramValidationError("perp_funding_capture_requires_short_direction")
        referenced: set[str] = set()
        for expression in (program.get("calculated_features") or {}).values():
            referenced.update(expression_names(str(expression)))
        for name in (
            "entry_expression",
            "invalidation_expression",
            "edge_expression",
            "score_expression",
        ):
            referenced.update(expression_names(str(program.get(name) or "")))
        missing = sorted(PERP_FUNDING_CAPTURE_REQUIRED_FEATURES - referenced)
        if missing:
            raise ProgramValidationError(
                "perp_funding_capture_missing_required_features:" + ",".join(missing)
            )
        return
    if output_trade_type != "global_proxy_shock_reversal":
        return
    universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
    venues = universe.get("venues")
    venue_values = venues if isinstance(venues, list) else [venues] if venues else []
    if {str(value).strip().upper() for value in venue_values} != {"YAHOO_PROXY"}:
        raise ProgramValidationError("shock_reversal_requires_yahoo_proxy_universe")
    calculated = program.get("calculated_features") or {}
    for name, expected in SHOCK_REVERSAL_CALCULATED_FEATURES.items():
        if name not in calculated or _canonical_expression(calculated[name]) != _canonical_expression(expected):
            raise ProgramValidationError(f"shock_reversal_invalid_{name}")
    for name, expected in SHOCK_REVERSAL_DIRECTION_EXPRESSIONS.items():
        if _canonical_expression(program.get(name)) != _canonical_expression(expected):
            raise ProgramValidationError(f"shock_reversal_invalid_{name}")
    entry_names = expression_names(str(program.get("entry_expression") or ""))
    if not {"shock_magnitude_bps", "shock_sigma", "flip_strength_bps"}.issubset(entry_names):
        raise ProgramValidationError("shock_reversal_entry_requires_shock_and_flip_features")


def _ordered_calculated_features(calculated: dict) -> tuple[dict[str, str], set[str]]:
    """Validate and dependency-order calculated features.

    Strategy contracts are persisted as sorted JSON, so mapping insertion order
    cannot define calculation order. References to another declared calculated
    feature are graph dependencies; only references outside the declared and
    runtime feature sets require feature code.
    """
    expressions: dict[str, str] = {}
    referenced_names: dict[str, set[str]] = {}
    for raw_name, raw_expression in calculated.items():
        name = str(raw_name)
        if not name.isidentifier() or name.startswith("_"):
            raise ProgramValidationError(f"invalid_feature_name:{name}")
        if name in expressions:
            raise ProgramValidationError(f"duplicate_feature_name:{name}")
        expression = str(raw_expression)
        expressions[name] = expression
        referenced_names[name] = expression_names(expression)

    declared = set(expressions)
    runtime_features = set(BASE_FEATURES | METADATA_NAMES)
    missing = {
        referenced
        for names in referenced_names.values()
        for referenced in names - declared - runtime_features
    }
    dependencies = {
        name: set(names & declared)
        for name, names in referenced_names.items()
    }
    dependents: dict[str, set[str]] = {name: set() for name in declared}
    for name, required in dependencies.items():
        for dependency in required:
            dependents[dependency].add(name)

    ready = sorted(name for name, required in dependencies.items() if not required)
    ordered_names: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered_names.append(name)
        for dependent in sorted(dependents[name]):
            dependencies[dependent].discard(name)
            if (
                not dependencies[dependent]
                and dependent not in ordered_names
                and dependent not in ready
            ):
                ready.append(dependent)
        ready.sort()

    if len(ordered_names) != len(expressions):
        cycle = sorted(name for name, required in dependencies.items() if required)
        raise ProgramValidationError("calculated_feature_dependency_cycle:" + ",".join(cycle))
    return {name: expressions[name] for name in ordered_names}, missing


def compile_observation_program(logic: dict) -> tuple[dict | None, dict]:
    program = dict(logic or {})
    program["type"] = LOGIC_TYPE
    calculated = program.get("calculated_features")
    if calculated is None:
        calculated = {}
    if not isinstance(calculated, dict) or len(calculated) > 32:
        return None, {"status": "invalid", "reason": "calculated_features_must_be_object"}
    available = set(BASE_FEATURES | METADATA_NAMES)
    missing: set[str] = set()
    try:
        ordered_calculated, calculated_missing = _ordered_calculated_features(calculated)
        missing.update(calculated_missing)
        available.update(ordered_calculated)
        expressions = {
            "entry_expression": str(program.get("entry_expression") or "True"),
            "invalidation_expression": str(program.get("invalidation_expression") or "False"),
            "long_expression": str(program.get("long_expression") or "False"),
            "short_expression": str(program.get("short_expression") or "False"),
            "edge_expression": str(program.get("edge_expression") or "0"),
            "score_expression": str(program.get("score_expression") or "50"),
        }
        direction = str(program.get("direction") or "").lower()
        if direction not in {"", "long", "short"}:
            raise ProgramValidationError("direction_must_be_long_or_short")
        if direction == "" and expressions["long_expression"] == "False" and expressions["short_expression"] == "False":
            raise ProgramValidationError("direction_expression_required")
        for expression in expressions.values():
            missing.update(expression_names(expression) - available)
        program["calculated_features"] = ordered_calculated
        program.update(expressions)
        program["route_surface"] = str(program.get("route_surface") or "auto").lower()
        if program["route_surface"] not in {
            "auto",
            "spot",
            "perp",
            "proxy",
            "prediction",
            NAV_REFERENCE_ROUTE_SURFACE,
            AUCTION_REFERENCE_ROUTE_SURFACE,
        }:
            raise ProgramValidationError("unsupported_route_surface")
        program["output_trade_type"] = str(program.get("output_trade_type") or "").lower()
        if program["output_trade_type"]:
            required_surface = OUTPUT_TRADE_TYPE_SURFACES.get(program["output_trade_type"])
            if required_surface is None:
                raise ProgramValidationError("unsupported_output_trade_type")
            if program["route_surface"] != required_surface:
                raise ProgramValidationError(
                    f"output_trade_type_requires_{required_surface}_route_surface"
                )
            _validate_output_trade_type_contract(program)
        signature = novelty_signature(program)
    except ProgramValidationError as exc:
        return None, {"status": "invalid", "reason": str(exc)}
    if missing:
        return None, {
            "status": "needs_feature_code",
            "reason": "missing_program_features",
            "missing_features": sorted(missing),
        }
    return program, {
        "status": "compiled",
        "reason": "observation_program_compiled",
        "novelty_signature": signature,
        "available_feature_count": len(available),
    }


def _universe_matches(frame: dict, universe: dict) -> bool:
    aliases = {
        "venues": "venue",
        "inst_ids": "inst_id",
        "trade_types": "trade_type",
        "asset_classes": "asset_class",
        "regions": "region",
        "market_types": "market_type",
        "market_surfaces": "market_surface",
        "quotes": "quote",
        "bases": "base",
    }
    for plural, field in aliases.items():
        allowed = universe.get(plural)
        if not allowed:
            continue
        values = {str(item).upper() for item in (allowed if isinstance(allowed, list) else [allowed])}
        if str(frame.get(field) or "").upper() not in values:
            return False
    return True


def _route_surface(frame: dict, program: dict) -> str:
    explicit = str(program.get("route_surface") or "auto").lower()
    if explicit != "auto":
        return explicit
    trade_type = str(frame.get("trade_type") or "").lower()
    market_type = str(frame.get("market_type") or "").lower()
    asset_class = str(frame.get("asset_class") or "").lower()
    if "prediction" in trade_type or market_type in {"prediction", "event"}:
        return "prediction"
    if market_type in {"perp", "future", "futures"} or "perp" in trade_type:
        return "perp"
    if market_type == "spot" or ("crypto" in asset_class and "derivative" not in asset_class):
        return "spot"
    return "proxy"


def _route_mapping(frame: dict, program: dict, side: str) -> tuple[str, str]:
    output_trade_type = str(program.get("output_trade_type") or "")
    if output_trade_type == "perp_funding_capture":
        direction = "funding_capture_short_perp" if side == "short" else "funding_capture_long_perp"
        return "perp_funding_basis", direction
    surface = _route_surface(frame, program)
    if surface == NAV_REFERENCE_ROUTE_SURFACE:
        # A reference NAV can support a directional paper measurement, but it
        # is never evidence of an executable order route.
        return "global_market_discovery_proxy", f"{side}_proxy"
    if surface == AUCTION_REFERENCE_ROUTE_SURFACE:
        # Auction results are paper research references, never order routes.
        return "global_market_discovery_proxy", f"{side}_proxy"
    if surface == "spot":
        return "frontier_crypto_venue_map", f"{side}_frontier_spot"
    if surface == "perp":
        return "frontier_crypto_venue_map", f"{side}_frontier_perp"
    if surface == "prediction":
        return "prediction_market_probability", "yes" if side == "long" else "no"
    if output_trade_type:
        return output_trade_type, f"{side}_proxy"
    source_type = str(frame.get("trade_type") or "")
    trade_type = source_type if source_type in {"global_proxy_momentum", "global_market_discovery_proxy"} else "global_market_discovery_proxy"
    return trade_type, f"{side}_proxy"


def _target_surface(frame: dict, program: dict) -> str:
    output_trade_type = str(program.get("output_trade_type") or "")
    universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
    requested_surfaces = universe.get("market_surfaces")
    market_surface = str(frame.get("market_surface") or "").strip().lower()
    if requested_surfaces and market_surface:
        requested = {
            str(value).strip().lower()
            for value in (
                requested_surfaces
                if isinstance(requested_surfaces, list)
                else [requested_surfaces]
            )
            if str(value).strip()
        }
        if market_surface in requested:
            # A program that explicitly selects a concrete market surface must
            # retain that provenance through paper routing.  Otherwise the
            # generic route surface (for example, "perp") would make an
            # exact same-surface contract impossible to evaluate.
            return market_surface
    if _route_surface(frame, program) in {
        NAV_REFERENCE_ROUTE_SURFACE,
        AUCTION_REFERENCE_ROUTE_SURFACE,
    }:
        return str(frame.get("market_surface") or "").strip().lower()
    return OUTPUT_TRADE_TYPE_TARGET_SURFACES.get(
        output_trade_type,
        _route_surface(frame, program),
    )


def _nav_reference_provenance(frame: dict) -> tuple[bool, list[str]]:
    """Validate the disclosed-NAV contract before making a paper label.

    Factsheet NAV observations are intentionally watch-only.  The reference
    route accepts only the exact public disclosure shape and remains isolated
    from ordinary order-ticket execution.
    """
    missing: list[str] = []
    if not str(frame.get("market_surface") or "").strip():
        missing.append("market_surface")
    if str(frame.get("quality_status") or "") != NAV_REFERENCE_QUALITY_STATUS:
        missing.append("quality_status")
    if str(frame.get("candidate_reject_reason") or "") != NAV_REFERENCE_REJECT_REASON:
        missing.append("candidate_reject_reason")
    if str(frame.get("freshness_state") or "") != "fresh":
        missing.append("freshness_state")
    if not str(frame.get("price_source") or "").strip():
        missing.append("price_source")
    return not missing, missing


def _auction_reference_provenance(frame: dict) -> tuple[bool, list[str]]:
    """Validate an official auction result before emitting a synthetic label."""
    missing: list[str] = []
    if not str(frame.get("market_surface") or "").strip():
        missing.append("market_surface")
    if str(frame.get("quality_status") or "") != AUCTION_REFERENCE_QUALITY_STATUS:
        missing.append("quality_status")
    reject_reason = str(frame.get("candidate_reject_reason") or "")
    if reject_reason not in {
        AUCTION_REFERENCE_REJECT_REASON,
        ALLOWANCE_AUCTION_REFERENCE_REJECT_REASON,
    }:
        missing.append("candidate_reject_reason")
    if str(frame.get("freshness_state") or "") != "fresh":
        missing.append("freshness_state")
    if not str(frame.get("price_source") or "").strip():
        missing.append("price_source")
    if reject_reason == ALLOWANCE_AUCTION_REFERENCE_REJECT_REASON:
        if str(frame.get("allowance_category") or "").strip().lower() != "current":
            missing.append("allowance_category")
        if not bool(frame.get("price_available")):
            missing.append("price_available")
        if _float(frame.get("auction_settlement_price_usd", frame.get("last"))) <= 0:
            missing.append("auction_settlement_price_usd")
        if not str(frame.get("event_date") or "").strip():
            missing.append("event_date")
        return not missing, missing
    if _float(frame.get("auction_term_days")) <= 0:
        missing.append("auction_term_days")
    if _float(frame.get("auction_average_yield_pct")) <= 0:
        missing.append("auction_average_yield_pct")
    if not str(frame.get("auction_at") or "").strip():
        missing.append("auction_at")
    return not missing, missing


def _icdx_milestone_companion_provenance(frame: dict) -> tuple[bool, list[str]]:
    """Validate ICDX milestone rows enriched with homepage companion pricing."""

    missing: list[str] = []
    if str(frame.get("market_surface") or "").strip() != "icdx_exchange_milestones":
        missing.append("market_surface")
    if str(frame.get("quality_status") or "") != "verified_proxy":
        missing.append("quality_status")
    if str(frame.get("candidate_reject_reason") or "") != "public_companion_price_requires_strategy_logic":
        missing.append("candidate_reject_reason")
    if str(frame.get("freshness_state") or "") != "fresh":
        missing.append("freshness_state")
    if str(frame.get("price_basis") or "") != "public_companion_cpotr_previous_settlement":
        missing.append("price_basis")
    if not str(frame.get("price_source") or "").strip():
        missing.append("price_source")
    if not str(frame.get("source_url") or "").strip():
        missing.append("source_url")
    if not str(frame.get("source_timeline_url") or "").strip():
        missing.append("source_timeline_url")
    if not str(frame.get("companion_inst_id") or "").strip():
        missing.append("companion_inst_id")
    if _float(frame.get("last")) <= 0:
        missing.append("last")
    if _float(frame.get("cpotr_price_card_pair_observed")) < 1.0:
        missing.append("cpotr_price_card_pair_observed")
    if _float(frame.get("suggested_opening_price")) <= 0:
        missing.append("suggested_opening_price")
    if _float(frame.get("previous_settlement_price")) <= 0:
        missing.append("previous_settlement_price")
    if not str(frame.get("contract_month") or "").strip():
        missing.append("contract_month")
    if _float(frame.get("years_since_cpotr_launch")) <= 0:
        missing.append("years_since_cpotr_launch")
    if _float(frame.get("years_since_gofx_launch")) <= 0:
        missing.append("years_since_gofx_launch")
    return not missing, missing


def _anp_opc_companion_provenance(frame: dict) -> tuple[bool, list[str]]:
    """Validate ANP OPC rows enriched with a Petrobras ADR companion quote."""

    missing: list[str] = []
    if str(frame.get("market_surface") or "").strip() != "anp_oferta_permanente_de_concessao":
        missing.append("market_surface")
    if str(frame.get("quality_status") or "") != "verified_proxy":
        missing.append("quality_status")
    if str(frame.get("candidate_reject_reason") or "") != "public_companion_price_requires_strategy_logic":
        missing.append("candidate_reject_reason")
    if str(frame.get("freshness_state") or "") != "fresh":
        missing.append("freshness_state")
    if str(frame.get("price_basis") or "") != "public_companion_petrobras_adr_quote":
        missing.append("price_basis")
    if not str(frame.get("price_source") or "").strip():
        missing.append("price_source")
    if not str(frame.get("source_url") or "").strip():
        missing.append("source_url")
    if not str(frame.get("source_programme_url") or "").strip():
        missing.append("source_programme_url")
    if not str(frame.get("companion_quote_symbol") or "").strip():
        missing.append("companion_quote_symbol")
    if _float(frame.get("last")) <= 0:
        missing.append("last")
    if max(
        _float(frame.get("available_exploratory_blocks")),
        _float(frame.get("new_exploratory_blocks")),
    ) <= 0:
        missing.append("opc_reference_signal")
    return not missing, missing


def _program_values(frame: dict, program: dict) -> dict:
    values = {key: frame.get(key) for key in BASE_FEATURES | METADATA_NAMES if frame.get(key) is not None}
    for name, expression in (program.get("calculated_features") or {}).items():
        values[name] = evaluate_expression(str(expression), values)
    return values


def generate_program_candidates(
    experiment: dict,
    frames: list[dict],
    settings: dict,
    *,
    max_candidates: int | None = None,
) -> tuple[list[dict], dict]:
    program, diagnostic = compile_observation_program(experiment.get("strategy_logic") or experiment.get("program") or {})
    if not program:
        return [], diagnostic
    universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
    risk_gates = experiment.get("risk_gates") if isinstance(experiment.get("risk_gates"), dict) else {}
    experimental_allocation = max(
        0.0,
        min(1.0, _float(risk_gates.get("paper_allocation_multiplier"), 1.0)),
    )
    exploration_mode = bool((settings.get("paper_exploration") or {}).get("enabled", False))
    synthetic_route_id = str(
        (settings.get("paper_exploration") or {}).get("synthetic_route_id")
        or "synthetic_research_paper"
    )
    generated: list[dict] = []
    rejects: dict[str, int] = defaultdict(int)
    lifecycle_diagnostics: dict[str, int] = defaultdict(int)
    limit = max_candidates or int(settings.get("strategy_lab", {}).get("max_candidates_per_experiment", 10))
    for frame in frames:
        if len(generated) >= limit:
            break
        route_surface = _route_surface(frame, program)
        closed_session_allowed = (
            str(frame.get("session_status") or "").lower() == "closed"
            and
            route_surface == AUCTION_REFERENCE_ROUTE_SURFACE
            and str(frame.get("quality_status") or "") == AUCTION_REFERENCE_QUALITY_STATUS
            and str(frame.get("candidate_reject_reason") or "") in {
                AUCTION_REFERENCE_REJECT_REASON,
                ALLOWANCE_AUCTION_REFERENCE_REJECT_REASON,
            }
        )
        if not _universe_matches(frame, universe):
            rejects["universe_mismatch"] += 1
            continue
        if (
            str(frame.get("session_status") or "").lower() in {"closed", "stale", "unavailable"}
            and not closed_session_allowed
        ):
            rejects["session_not_open"] += 1
            continue
        is_nav_reference = route_surface == NAV_REFERENCE_ROUTE_SURFACE
        is_auction_reference = route_surface == AUCTION_REFERENCE_ROUTE_SURFACE
        # EEX's public DataSource secondary-spot feed reports completed
        # trades. It supplies a defensible public price for research but no
        # executable quote, so preserve it on the generic synthetic research
        # route even if a broad route resolver happens to know the venue.
        is_reported_spot_reference = (
            str(frame.get("venue") or "").upper() == "EEX"
            and str(frame.get("market_surface") or "") == "eex_eu_ets_secondary_spot_trades"
            and str(frame.get("quality_status") or "") == "official_reported_trade"
            and str(frame.get("candidate_reject_reason") or "")
            == "reported_spot_trade_not_executable_quote"
        )
        is_icdx_cpotr_price_card_reference = (
            str(frame.get("venue") or "").upper() == "ICDX"
            and str(frame.get("market_surface") or "") == "icdx_cpotr"
            and str(frame.get("trade_type") or "") == "official_price_card_reference"
            and str(frame.get("quality_status") or "") == "official_price_card"
            and str(frame.get("candidate_reject_reason") or "")
            == "public_price_card_not_execution_route"
        )
        is_icdx_milestone_companion_reference = (
            str(frame.get("venue") or "").upper() == "ICDX"
            and str(frame.get("market_surface") or "") == "icdx_exchange_milestones"
            and str(frame.get("trade_type") or "") == "official_market_milestone_reference"
            and str(frame.get("quality_status") or "") == "verified_proxy"
            and str(frame.get("candidate_reject_reason") or "")
            == "public_companion_price_requires_strategy_logic"
        )
        is_anp_opc_companion_reference = (
            str(frame.get("venue") or "").upper() == "ANP_BRAZIL_OPC"
            and str(frame.get("market_surface") or "") == "anp_oferta_permanente_de_concessao"
            and str(frame.get("trade_type") or "") == "official_regulatory_programme_reference"
            and str(frame.get("quality_status") or "") == "verified_proxy"
            and str(frame.get("candidate_reject_reason") or "")
            == "public_companion_price_requires_strategy_logic"
        )
        icdx_cpotr_provenance_valid = bool(
            is_icdx_cpotr_price_card_reference
            and str(frame.get("freshness_state") or "") == "fresh"
            and _float(frame.get("cpotr_price_card_pair_observed")) >= 1.0
            and _float(frame.get("suggested_opening_price")) > 0
            and _float(frame.get("previous_settlement_price")) > 0
            and str(frame.get("price_type") or "") in {"suggested_opening", "previous_settlement"}
            and str(frame.get("contract_month") or "").strip()
            and str(frame.get("price_source") or "").strip()
        )
        icdx_milestone_provenance_valid = bool(
            is_icdx_milestone_companion_reference
            and _icdx_milestone_companion_provenance(frame)[0]
        )
        anp_opc_provenance_valid = bool(
            is_anp_opc_companion_reference
            and _anp_opc_companion_provenance(frame)[0]
        )
        provenance_valid = True
        provenance_missing: list[str] = []
        if is_nav_reference:
            provenance_valid, provenance_missing = _nav_reference_provenance(frame)
            if not provenance_valid:
                rejects["nav_reference_provenance_invalid"] += 1
                for field in provenance_missing:
                    rejects[f"nav_reference_missing:{field}"] += 1
                continue
        if is_auction_reference:
            provenance_valid, provenance_missing = _auction_reference_provenance(frame)
            if not provenance_valid:
                rejects["auction_reference_provenance_invalid"] += 1
                for field in provenance_missing:
                    rejects[f"auction_reference_missing:{field}"] += 1
                continue
        if is_icdx_milestone_companion_reference:
            provenance_valid, provenance_missing = _icdx_milestone_companion_provenance(frame)
            if not provenance_valid:
                rejects["icdx_milestone_reference_provenance_invalid"] += 1
                for field in provenance_missing:
                    rejects[f"icdx_milestone_reference_missing:{field}"] += 1
                continue
        if is_anp_opc_companion_reference:
            provenance_valid, provenance_missing = _anp_opc_companion_provenance(frame)
            if not provenance_valid:
                rejects["anp_opc_reference_provenance_invalid"] += 1
                for field in provenance_missing:
                    rejects[f"anp_opc_reference_missing:{field}"] += 1
                continue
        try:
            values = _program_values(frame, program)
            if not bool(evaluate_expression(program["entry_expression"], values)):
                rejects["entry_expression_false"] += 1
                continue
            invalidation_active_at_entry = bool(
                evaluate_expression(program["invalidation_expression"], values)
            )
            if invalidation_active_at_entry:
                lifecycle_diagnostics["invalidation_active_at_entry"] += 1
            side = str(program.get("direction") or "").lower()
            if not side:
                long_signal = bool(evaluate_expression(program["long_expression"], values))
                short_signal = bool(evaluate_expression(program["short_expression"], values))
                if long_signal == short_signal:
                    rejects["ambiguous_or_empty_direction"] += 1
                    continue
                side = "long" if long_signal else "short"
            edge = _float(evaluate_expression(program["edge_expression"], values))
            score = max(0.0, min(100.0, _float(evaluate_expression(program["score_expression"], values), 50.0)))
        except (ProgramValidationError, ArithmeticError, ValueError, TypeError, OverflowError):
            rejects["expression_runtime_error"] += 1
            continue
        non_positive_edge_at_entry = edge <= 0
        if non_positive_edge_at_entry:
            lifecycle_diagnostics["non_positive_cost_adjusted_edge_at_entry"] += 1
            if not exploration_mode:
                rejects["non_positive_cost_adjusted_edge"] += 1
                continue
        target_surface = _target_surface(frame, program)
        source_trade_type = str(frame.get("trade_type") or "")
        trade_type, direction = _route_mapping(frame, program, side)
        signature = str(diagnostic.get("novelty_signature") or novelty_signature(program))
        candidate = {
            "venue": str(frame.get("venue") or "UNKNOWN"),
            "inst_id": str(frame.get("inst_id")),
            "direction": direction,
            "trade_type": trade_type,
            "target_surface": target_surface,
            "score": round(score, 3),
            "liquidity_score": max(0.0, min(1.0, _float(frame.get("liquidity_score"), 0.5))),
            "spread_bps": max(0.0, _float(frame.get("spread_bps"))),
            "last": _float(frame.get("last")),
            "edge_bps_estimate": round(edge, 3),
            "change_24h_pct": _float(frame.get("change_24h_pct")),
            "stale_minutes": max(0.0, _float(frame.get("stale_minutes"))),
            "freshness_age_seconds": max(
                0.0,
                _float(
                    frame.get("freshness_age_seconds"),
                    _float(frame.get("provider_age_seconds"), _float(frame.get("stale_minutes")) * 60.0),
                ),
            ),
            "quote_volume_24h": max(0.0, _float(frame.get("quote_volume_24h"))),
            "proxy_depth_notional_usd": max(
                0.0,
                _float(frame.get("proxy_depth_notional_usd"), _float(frame.get("quote_volume_24h"))),
            ),
            "proxy_venue_health_status": str(
                frame.get("proxy_venue_health_status")
                or frame.get("venue_health_status")
                or frame.get("data_status")
                or ""
            ),
            "funding_bps": _float(frame.get("funding_bps")),
            "basis_bps": _float(frame.get("basis_bps")),
            "seen_at": str(frame.get("observed_at") or dt.datetime.now(dt.timezone.utc).isoformat()),
            "data_status": str(frame.get("data_status") or "reachable"),
            "quality_score": frame.get("quality_score"),
            "quality_status": frame.get("quality_status"),
            "asset_class": frame.get("asset_class"),
            "region": frame.get("region"),
            "base": frame.get("base"),
            "quote": frame.get("quote"),
            "paper_only": True,
            "synthetic_research_paper": (
                is_auction_reference
                or is_reported_spot_reference
                or is_icdx_cpotr_price_card_reference
                or is_icdx_milestone_companion_reference
                or is_anp_opc_companion_reference
            ),
            "paper_reported_spot_reference": is_reported_spot_reference,
            "paper_cpotr_price_card_reference": is_icdx_cpotr_price_card_reference,
            "paper_icdx_milestone_reference": is_icdx_milestone_companion_reference,
            "paper_anp_opc_reference": is_anp_opc_companion_reference,
            "synthetic_route_id": (
                synthetic_route_id
                if (
                    is_reported_spot_reference
                    or is_icdx_cpotr_price_card_reference
                    or is_icdx_milestone_companion_reference
                    or is_anp_opc_companion_reference
                )
                else None
            ),
            "synthetic_not_live_equivalent": (
                is_auction_reference
                or is_reported_spot_reference
                or is_icdx_cpotr_price_card_reference
                or is_icdx_milestone_companion_reference
                or is_anp_opc_companion_reference
            ),
            "paper_execution_semantics": (
                "synthetic_research_not_live_equivalent"
                if (
                    is_reported_spot_reference
                    or is_icdx_cpotr_price_card_reference
                    or is_icdx_milestone_companion_reference
                    or is_anp_opc_companion_reference
                )
                else None
            ),
            "signal_stats_scope": (
                "synthetic_research"
                if (
                    is_auction_reference
                    or is_reported_spot_reference
                    or is_icdx_cpotr_price_card_reference
                    or is_icdx_milestone_companion_reference
                    or is_anp_opc_companion_reference
                )
                else None
            ),
            "paper_nav_reference": is_nav_reference,
            "paper_nav_reference_provenance_valid": provenance_valid,
            "paper_nav_reference_provenance": {
                "market_surface": str(frame.get("market_surface") or ""),
                "quality_status": str(frame.get("quality_status") or ""),
                "candidate_reject_reason": str(frame.get("candidate_reject_reason") or ""),
                "freshness_state": str(frame.get("freshness_state") or ""),
                "price_source": str(frame.get("price_source") or ""),
            }
            if is_nav_reference
            else {},
            "paper_auction_reference": is_auction_reference,
            "paper_auction_reference_provenance_valid": provenance_valid,
            "paper_auction_reference_provenance": {
                "market_surface": str(frame.get("market_surface") or ""),
                "quality_status": str(frame.get("quality_status") or ""),
                "source_quality_status": str(frame.get("source_quality_status") or ""),
                "candidate_reject_reason": str(frame.get("candidate_reject_reason") or ""),
                "freshness_state": str(frame.get("freshness_state") or ""),
                "price_source": str(frame.get("price_source") or ""),
                "auction_at": str(frame.get("auction_at") or ""),
                "auction_term_days": _float(frame.get("auction_term_days")),
                "auction_average_yield_pct": _float(frame.get("auction_average_yield_pct")),
                "auction_stop_out_yield_pct": _float(frame.get("auction_stop_out_yield_pct")),
                "isin": str(frame.get("isin") or ""),
                "maturity_date_iso": str(frame.get("maturity_date_iso") or ""),
                "allowance_category": str(frame.get("allowance_category") or ""),
                "auction_number": str(frame.get("auction_number") or ""),
                "event_date": str(frame.get("event_date") or ""),
                "price_available": bool(frame.get("price_available")),
                "reserve_sale": bool(frame.get("reserve_sale")),
                "auction_settlement_price_usd": _float(
                    frame.get("auction_settlement_price_usd", frame.get("last"))
                ),
            }
            if is_auction_reference
            else {},
            "paper_cpotr_price_card_provenance_valid": icdx_cpotr_provenance_valid,
            "paper_cpotr_price_card_provenance": {
                "market_surface": str(frame.get("market_surface") or ""),
                "quality_status": str(frame.get("quality_status") or ""),
                "candidate_reject_reason": str(frame.get("candidate_reject_reason") or ""),
                "freshness_state": str(frame.get("freshness_state") or ""),
                "price_source": str(frame.get("price_source") or ""),
                "contract_month": str(frame.get("contract_month") or ""),
                "price_type": str(frame.get("price_type") or ""),
                "price_basis": str(frame.get("price_basis") or ""),
                "suggested_opening_price": _float(frame.get("suggested_opening_price")),
                "previous_settlement_price": _float(frame.get("previous_settlement_price")),
                "cpotr_opening_gap_bps": _float(frame.get("cpotr_opening_gap_bps")),
            }
            if is_icdx_cpotr_price_card_reference
            else {},
            "paper_icdx_milestone_provenance_valid": icdx_milestone_provenance_valid,
            "paper_icdx_milestone_provenance": {
                "market_surface": str(frame.get("market_surface") or ""),
                "quality_status": str(frame.get("quality_status") or ""),
                "candidate_reject_reason": str(frame.get("candidate_reject_reason") or ""),
                "freshness_state": str(frame.get("freshness_state") or ""),
                "price_source": str(frame.get("price_source") or ""),
                "price_basis": str(frame.get("price_basis") or ""),
                "source_url": str(frame.get("source_url") or ""),
                "source_timeline_url": str(frame.get("source_timeline_url") or ""),
                "contract_month": str(frame.get("contract_month") or ""),
                "companion_inst_id": str(frame.get("companion_inst_id") or ""),
                "exchange_established_year": _float(frame.get("exchange_established_year")),
                "cpotr_launch_year": _float(frame.get("cpotr_launch_year")),
                "gofx_launch_year": _float(frame.get("gofx_launch_year")),
                "years_since_cpotr_launch": _float(frame.get("years_since_cpotr_launch")),
                "years_since_gofx_launch": _float(frame.get("years_since_gofx_launch")),
                "cpotr_opening_gap_bps": _float(frame.get("cpotr_opening_gap_bps")),
            }
            if is_icdx_milestone_companion_reference
            else {},
            "paper_anp_opc_provenance_valid": anp_opc_provenance_valid,
            "paper_anp_opc_provenance": {
                "market_surface": str(frame.get("market_surface") or ""),
                "quality_status": str(frame.get("quality_status") or ""),
                "candidate_reject_reason": str(frame.get("candidate_reject_reason") or ""),
                "freshness_state": str(frame.get("freshness_state") or ""),
                "price_source": str(frame.get("price_source") or ""),
                "price_basis": str(frame.get("price_basis") or ""),
                "source_url": str(frame.get("source_url") or ""),
                "source_programme_url": str(frame.get("source_programme_url") or ""),
                "companion_quote_symbol": str(frame.get("companion_quote_symbol") or ""),
                "available_exploratory_blocks": _float(frame.get("available_exploratory_blocks")),
                "new_exploratory_blocks": _float(frame.get("new_exploratory_blocks")),
                "offshore_new_blocks": _float(frame.get("offshore_new_blocks")),
                "onshore_new_blocks": _float(frame.get("onshore_new_blocks")),
            }
            if is_anp_opc_companion_reference
            else {},
            "strategy_lab_id": str(experiment.get("strategy_lab_id")),
            "strategy_lab_version": int(experiment.get("version") or 1),
            "strategy_lab_experiment_type": "market_strategy",
            "strategy_lab_hypothesis": str(experiment.get("hypothesis") or ""),
            "strategy_lab_logic_type": LOGIC_TYPE,
            "strategy_lab_candidate": True,
            "strategy_lab_source_trade_type": source_trade_type,
            "strategy_lab_output_trade_type": trade_type,
            "strategy_lab_program_signature": signature,
            "strategy_lab_invalidation_expression": program["invalidation_expression"],
            "strategy_lab_invalidation_active_at_entry": invalidation_active_at_entry,
            "strategy_lab_non_positive_edge_at_entry": non_positive_edge_at_entry,
            "strategy_lab_contract_warnings": [
                warning
                for warning, applies in (
                    ("entry_invalidation_overlap", invalidation_active_at_entry),
                    ("non_positive_cost_adjusted_edge", non_positive_edge_at_entry),
                )
                if applies
            ],
            "strategy_lab_contract_warning": (
                "entry_invalidation_overlap"
                if invalidation_active_at_entry
                else "non_positive_cost_adjusted_edge"
                if non_positive_edge_at_entry
                else None
            ),
            "promotion_eligible": not (
                invalidation_active_at_entry
                or non_positive_edge_at_entry
                or is_auction_reference
                or is_reported_spot_reference
                or is_icdx_cpotr_price_card_reference
                or is_icdx_milestone_companion_reference
                or is_anp_opc_companion_reference
            ),
            "strategy_reliability_allocation_multiplier": experimental_allocation,
            "strategy_lab_relaxation": risk_gates.get("adaptive_relaxation") or {},
            "strategy_lab_program_features": {
                key: values.get(key)
                for key in sorted(set(program.get("calculated_features") or {}) | BASE_FEATURES)
                if key in values
            },
            "signal_lineage_key": f"STRATEGY_LAB_PROGRAM|{experiment.get('strategy_lab_id')}|v{experiment.get('version', 1)}",
            "thesis": f"Strategy Lab observation program {experiment.get('strategy_lab_id')}: {experiment.get('hypothesis', '')}"[:1000],
        }
        for field in PROGRAM_CANDIDATE_PASSTHROUGH_FIELDS:
            if frame.get(field) is not None:
                candidate[field] = frame[field]
        generated.append(candidate)
    generated.sort(key=lambda row: (float(row.get("score") or 0), float(row.get("edge_bps_estimate") or 0)), reverse=True)
    return generated[:limit], {
        **diagnostic,
        "source_observation_count": len(frames),
        "generated_candidate_count": min(len(generated), limit),
        "reject_reasons": dict(rejects),
        "lifecycle_diagnostic_counts": dict(lifecycle_diagnostics),
    }


def candidate_parity_key(candidate: dict) -> tuple:
    return (
        str(candidate.get("venue")),
        str(candidate.get("inst_id")),
        str(candidate.get("trade_type")),
        str(candidate.get("direction")),
        round(_float(candidate.get("edge_bps_estimate")), 6),
        round(_float(candidate.get("score")), 6),
    )


def assert_plugin_parity(plugin: Any, experiment: dict, frames: list[dict], settings: dict) -> None:
    expected, _ = generate_program_candidates(experiment, frames, settings)
    context = {"settings": settings, "strategy_lab_experiment": experiment, "feature_frames": frames}
    actual = plugin.generate(frames, context=context)
    if sorted(map(candidate_parity_key, actual)) != sorted(map(candidate_parity_key, expected)):
        raise AssertionError("generated signal plugin does not reproduce its Strategy Lab observation program")
