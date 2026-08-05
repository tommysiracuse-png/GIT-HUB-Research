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
        if program["route_surface"] not in {"auto", "spot", "perp", "proxy", "prediction"}:
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
    return OUTPUT_TRADE_TYPE_TARGET_SURFACES.get(
        output_trade_type,
        _route_surface(frame, program),
    )


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
    generated: list[dict] = []
    rejects: dict[str, int] = defaultdict(int)
    lifecycle_diagnostics: dict[str, int] = defaultdict(int)
    limit = max_candidates or int(settings.get("strategy_lab", {}).get("max_candidates_per_experiment", 10))
    for frame in frames:
        if len(generated) >= limit:
            break
        if not _universe_matches(frame, universe):
            rejects["universe_mismatch"] += 1
            continue
        if str(frame.get("session_status") or "").lower() in {"closed", "stale", "unavailable"}:
            rejects["session_not_open"] += 1
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
                invalidation_active_at_entry or non_positive_edge_at_entry
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
