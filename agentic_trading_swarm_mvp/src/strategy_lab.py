"""Paper-only Strategy Lab experiments.

The lab is an R&D layer for LLM-invented strategy ideas. It does not execute
arbitrary model code. Accepted ideas become bounded contracts that select or
transform existing radar candidates, then the normal route/review/paper engine
decides whether they deserve paper trades.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from storage import RUNS_DIR, add_llm_recommendation, utc_now


REPORT_JSON = RUNS_DIR / "strategy_lab_report.json"
REPORT_MD = RUNS_DIR / "strategy_lab_report.md"

ACTIVE_STATUSES = {"active_testing"}
TRACKED_STATUSES = {
    "proposed",
    "active_testing",
    "needs_data",
    "needs_route",
    "needs_more_evidence",
    "split_into_children",
    "promote_candidate",
    "promotion_queued",
    "promoted_to_code",
    "retired_bad_evidence",
    "retired_no_activity",
    "rejected_invalid",
}
EXPERIMENT_TYPES = {
    "market_strategy",
    "risk_filter",
    "execution_filter",
    "system_repair",
    "reporting_quality",
}
DEFAULT_EXPERIMENT_TYPE = "market_strategy"
SUPPORTED_LOGIC_TYPES = {"candidate_filter", "candidate_selector", "candidate_transform"}
FALLBACK_TRADE_TYPE_EXAMPLES = {
    "frontier_crypto_venue_map",
    "perp_funding_basis",
    "global_market_discovery_proxy",
    "global_proxy_momentum",
    "prediction_market_probability",
}
FALLBACK_DIRECTION_EXAMPLES = {
    "long_frontier_spot",
    "short_frontier_spot",
    "long_frontier_perp",
    "short_frontier_perp",
    "long_proxy",
    "short_proxy",
    "funding_capture_long_perp",
    "funding_capture_short_perp",
    "long_perp_short_spot",
    "short_perp_long_spot",
    "basis_mean_reversion_long_perp",
    "basis_mean_reversion_short_perp",
    "yes",
    "no",
}
GENERIC_DIRECTIONS = {"long", "short"}
FALLBACK_DIRECTION_TRADE_TYPE_HINTS = {
    "long_frontier_spot": "frontier_crypto_venue_map",
    "short_frontier_spot": "frontier_crypto_venue_map",
    "long_frontier_perp": "frontier_crypto_venue_map",
    "short_frontier_perp": "frontier_crypto_venue_map",
    "funding_capture_long_perp": "perp_funding_basis",
    "funding_capture_short_perp": "perp_funding_basis",
    "long_perp_short_spot": "perp_funding_basis",
    "short_perp_long_spot": "perp_funding_basis",
    "basis_mean_reversion_long_perp": "perp_funding_basis",
    "basis_mean_reversion_short_perp": "perp_funding_basis",
    "yes": "prediction_market_probability",
    "no": "prediction_market_probability",
}


def _json_loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _utc() -> str:
    return utc_now()


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(created_at: str | None) -> float:
    created = _parse_time(created_at)
    if not created:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600.0)


def _slug(text: str, fallback: str = "strategy_lab") -> str:
    cleaned = []
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "_":
            cleaned.append("_")
    slug = "".join(cleaned).strip("_")[:64]
    return slug or fallback


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _unique(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value not in seen:
            output.append(value)
            seen.add(value)
    return output


def _runtime_strategy_vocabulary(candidates: list[dict]) -> dict:
    direction_trade_types: dict[str, Counter] = defaultdict(Counter)
    trade_types = set()
    directions = set()
    for candidate in candidates:
        trade_type = str(candidate.get("trade_type") or "").strip()
        direction = str(candidate.get("direction") or "").strip()
        if trade_type:
            trade_types.add(trade_type)
        if direction:
            directions.add(direction)
        if trade_type and direction:
            direction_trade_types[direction][trade_type] += 1
    candidate_fields = Counter(
        str(field)
        for candidate in candidates
        for field, value in candidate.items()
        if value is not None
    )
    for field in ("quality_score", "stale_minutes", "timestamp", "price", "region", "asset_class"):
        candidate_fields[field] = sum(
            _candidate_field_value(candidate, field) is not None
            for candidate in candidates
        )
    return {
        "trade_types": trade_types,
        "directions": directions,
        "venues": {str(candidate.get("venue")) for candidate in candidates if candidate.get("venue")},
        "regions": {
            str(_candidate_region(candidate))
            for candidate in candidates
            if _candidate_region(candidate)
        },
        "asset_classes": {
            str(_candidate_asset_class(candidate))
            for candidate in candidates
            if _candidate_asset_class(candidate)
        },
        "candidate_fields": set(candidate_fields),
        "candidate_field_counts": candidate_fields,
        "direction_trade_type_hints": {
            direction: counter.most_common(1)[0][0]
            for direction, counter in direction_trade_types.items()
            if counter
        },
    }


def _match_observed_token(token: str, observed: set[str]) -> str | None:
    lowered = token.lower()
    for item in observed:
        if item.lower() == lowered:
            return item
    return None


def _normalize_strategy_logic(logic: dict, vocabulary: dict | None = None) -> dict:
    normalized = dict(logic)
    notes = _as_list(normalized.get("normalization_notes"))
    vocabulary = vocabulary or {}
    observed_trade_types = set(vocabulary.get("trade_types") or set())
    observed_directions = set(vocabulary.get("directions") or set())
    direction_trade_type_hints = dict(vocabulary.get("direction_trade_type_hints") or {})
    direction_trade_type_hints.update(
        {
            direction: direction_trade_type_hints.get(direction, trade_type)
            for direction, trade_type in FALLBACK_DIRECTION_TRADE_TYPE_HINTS.items()
        }
    )
    known_trade_types_lower = {item.lower() for item in observed_trade_types | FALLBACK_TRADE_TYPE_EXAMPLES}
    known_directions_lower = {item.lower() for item in observed_directions | FALLBACK_DIRECTION_EXAMPLES | GENERIC_DIRECTIONS}

    trade_types = _as_list(normalized.get("trade_types") or normalized.get("source_trade_types") or normalized.get("trade_type"))
    directions = _as_list(normalized.get("directions") or normalized.get("allowed_directions") or normalized.get("direction"))

    cleaned_trade_types: list[str] = []
    repaired_directions = list(directions)
    for item in trade_types:
        token = item.strip()
        lowered = token.lower()
        observed_direction = _match_observed_token(token, observed_directions)
        if observed_direction or lowered in known_directions_lower:
            repaired = observed_direction or lowered
            repaired_directions.append(repaired)
            notes.append(f"moved_trade_type_direction:{repaired}")
            continue
        if lowered in GENERIC_DIRECTIONS:
            repaired_directions.append(lowered)
            notes.append(f"moved_trade_type_generic_direction:{lowered}")
            continue
        observed_trade_type = _match_observed_token(token, observed_trade_types)
        cleaned_trade_types.append(observed_trade_type or (lowered if lowered in known_trade_types_lower else token))

    normalized_directions: list[str] = []
    for item in repaired_directions:
        token = item.strip()
        lowered = token.lower()
        observed_direction = _match_observed_token(token, observed_directions)
        normalized_directions.append(observed_direction or (lowered if lowered in known_directions_lower else token))

    inferred_trade_types = [
        direction_trade_type_hints[direction]
        for direction in normalized_directions
        if direction in direction_trade_type_hints
    ]
    if not cleaned_trade_types and inferred_trade_types:
        cleaned_trade_types.extend(inferred_trade_types)
        notes.append("inferred_trade_type_from_direction")

    venues = _as_list(normalized.get("venues") or normalized.get("allowed_venues"))
    if venues:
        normalized["venues"] = _unique([venue.strip().upper() for venue in venues if venue.strip()])
    if cleaned_trade_types:
        normalized["trade_types"] = _unique(cleaned_trade_types)
    elif "trade_types" in normalized:
        normalized["trade_types"] = []
    if normalized_directions:
        normalized["directions"] = _unique(normalized_directions)
    elif "directions" in normalized:
        normalized["directions"] = []
    required_fields = _as_list(normalized.get("required_fields"))
    scope_metadata_fields = {
        "venue",
        "venues",
        "trade_type",
        "trade_types",
        "direction",
        "directions",
        "region",
        "regions",
        "asset_class",
        "asset_classes",
    }
    removed_scope_fields = [field for field in required_fields if field in scope_metadata_fields]
    if removed_scope_fields:
        normalized["required_fields"] = [field for field in required_fields if field not in scope_metadata_fields]
        notes.append("removed_scope_metadata_from_required_fields:" + ",".join(removed_scope_fields))
    if notes:
        normalized["normalization_notes"] = _unique(notes)
    return normalized


def _has_strategy_scope(logic: dict) -> bool:
    if bool(logic.get("allow_any_surface")):
        return True
    return any(
        _as_list(logic.get(key))
        for key in (
            "trade_types",
            "source_trade_types",
            "venues",
            "allowed_venues",
            "directions",
            "allowed_directions",
            "regions",
            "allowed_regions",
            "asset_classes",
            "allowed_asset_classes",
        )
    )


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_dict(*values: Any) -> dict:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return {}


def _normalize_experiment_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in EXPERIMENT_TYPES else None


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _text_blob(*values: Any) -> str:
    pieces: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            pieces.append(json.dumps(value, sort_keys=True, default=str))
        else:
            pieces.append(str(value))
    return " ".join(pieces).lower()


def _classify_experiment_type(contract: dict, payload: dict, logic: dict) -> str:
    explicit = (
        _normalize_experiment_type(contract.get("experiment_type"))
        or _normalize_experiment_type(contract.get("strategy_lab_experiment_type"))
        or _normalize_experiment_type(payload.get("experiment_type"))
        or _normalize_experiment_type(payload.get("strategy_lab_experiment_type"))
    )
    if explicit:
        return explicit

    semantic_logic = {key: value for key, value in logic.items() if key not in {"type", "logic_type"}}
    text = _text_blob(
        contract.get("strategy_lab_id"),
        contract.get("hypothesis"),
        payload.get("title"),
        payload.get("rationale"),
        payload.get("market_key"),
        payload.get("signal_key"),
        semantic_logic,
    )
    has_surface = _has_strategy_scope(logic)
    alpha_terms = (
        "capture",
        "carry",
        "arbitrage",
        "mean reversion",
        "mean-reversion",
        "momentum",
        "dislocation",
        "reversal",
        "continuation",
        "spread trade",
        "basis",
        "funding",
        "regional",
        "proxy",
        "prediction",
        "event",
        "odds",
        "frontier",
    )
    repair_terms = (
        "malformed",
        "schema",
        "json",
        "parse",
        "parser",
        "recommendation output",
        "proposal format",
        "serialization",
        "code evolution",
        "patch",
        "invalid path",
        "test command",
    )
    reporting_terms = ("report", "dashboard", "packet", "visibility", "summary", "markdown")
    filter_terms = (
        "filter",
        "gate",
        "tighten",
        "demote",
        "cooldown",
        "quarantine",
        "block",
        "cap",
        "false positive",
        "decay",
        "risk",
        "weak",
        "failing",
    )
    execution_terms = (
        "route",
        "borrow",
        "slippage",
        "spread",
        "liquidity",
        "order book",
        "order-book",
        "freshness",
        "quality gate",
        "execution",
    )

    if _contains_any(text, repair_terms) and not has_surface:
        return "system_repair"
    if _contains_any(text, reporting_terms) and not has_surface:
        return "reporting_quality"
    if _contains_any(text, filter_terms) and not has_surface:
        return "risk_filter"
    if has_surface and _contains_any(text, alpha_terms):
        return "market_strategy"
    if _contains_any(text, execution_terms) and _contains_any(text, filter_terms):
        return "execution_filter"
    if _contains_any(text, filter_terms):
        return "risk_filter"
    if has_surface or _contains_any(text, alpha_terms):
        return "market_strategy"
    if _contains_any(text, repair_terms):
        return "system_repair"
    if _contains_any(text, reporting_terms):
        return "reporting_quality"
    return DEFAULT_EXPERIMENT_TYPE


def _resolved_experiment_type(stored: Any, item: dict, logic: dict) -> str:
    stored_type = _normalize_experiment_type(stored)
    if stored_type and stored_type != DEFAULT_EXPERIMENT_TYPE:
        return stored_type
    infer_item = dict(item)
    infer_item.pop("experiment_type", None)
    inferred = _classify_experiment_type(infer_item, infer_item, logic)
    if inferred != DEFAULT_EXPERIMENT_TYPE:
        return inferred
    return stored_type or inferred


def _contract_from_payload(payload: dict) -> dict:
    proposed = payload.get("proposed_change")
    contract = _first_dict(
        payload.get("strategy_lab_experiment"),
        payload.get("strategy_contract"),
        payload.get("strategy_lab"),
        proposed,
    )
    if isinstance(proposed, dict):
        contract = _first_dict(
            proposed.get("strategy_lab_experiment"),
            proposed.get("strategy_contract"),
            proposed.get("strategy_lab"),
            contract,
        )
    if not contract:
        contract = {
            "hypothesis": str(proposed or payload.get("rationale") or payload.get("title") or "").strip(),
            "strategy_logic": {},
        }
    return contract


def _validate_contract(payload: dict) -> tuple[dict | None, str | None]:
    contract = _contract_from_payload(payload)
    hypothesis = str(contract.get("hypothesis") or payload.get("rationale") or payload.get("title") or "").strip()
    if not hypothesis:
        return None, "missing_hypothesis"

    logic = _first_dict(
        contract.get("strategy_logic"),
        contract.get("strategy_logic_json"),
        contract.get("logic"),
    )
    logic_type = str(logic.get("type") or logic.get("logic_type") or "candidate_filter")
    if logic_type not in SUPPORTED_LOGIC_TYPES:
        status = "rejected_invalid"
    else:
        status = "proposed"
    logic["type"] = logic_type
    logic = _normalize_strategy_logic(logic)
    if not _has_strategy_scope(logic):
        status = "needs_data"
    experiment_type = _classify_experiment_type(contract, payload, logic)
    if experiment_type != "market_strategy":
        status = "rejected_invalid"

    strategy_lab_id = str(contract.get("strategy_lab_id") or "").strip()
    if not strategy_lab_id:
        digest = hashlib.sha256(
            json.dumps({"hypothesis": hypothesis, "logic": logic}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:8]
        strategy_lab_id = f"{_slug(str(payload.get('title') or hypothesis))}_{digest}"

    version = max(1, _as_int(contract.get("version"), 1))
    risk_gates = _first_dict(contract.get("risk_gates"), contract.get("risk_gates_json"))
    promotion_rules = _first_dict(contract.get("promotion_rules"), contract.get("promotion_rules_json"))
    data_requirements = _first_dict(contract.get("data_requirements"), contract.get("data_requirements_json"))
    parent = contract.get("parent_strategy_lab_id")

    return {
        "strategy_lab_id": strategy_lab_id,
        "version": version,
        "parent_strategy_lab_id": str(parent).strip() if parent else None,
        "experiment_type": experiment_type,
        "status": status,
        "hypothesis": hypothesis,
        "strategy_logic": logic,
        "data_requirements": data_requirements,
        "risk_gates": risk_gates,
        "promotion_rules": promotion_rules,
    }, None


def ingest_strategy_lab_recommendation(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = dict(rec.get("payload") or {})
    contract, error = _validate_contract(payload)
    if contract is None:
        return [
            {
                "action_status": "skipped",
                "artifact": "strategy_lab_experiment",
                "reason": error or "invalid_contract",
            }
        ]

    now = _utc()
    values = (
        contract["strategy_lab_id"],
        contract["version"],
        contract["parent_strategy_lab_id"],
        contract["experiment_type"],
        contract["status"],
        contract["hypothesis"],
        json.dumps(contract["strategy_logic"], sort_keys=True),
        json.dumps(contract["strategy_logic"], sort_keys=True),
        "uncompiled",
        json.dumps(
            {
                "reason": (
                    "non_market_experiment_routed_outside_strategy_lab"
                    if contract["experiment_type"] != "market_strategy"
                    else "awaiting_runtime_contract_compilation"
                )
            },
            sort_keys=True,
        ),
        json.dumps(contract["data_requirements"], sort_keys=True),
        json.dumps(contract["risk_gates"], sort_keys=True),
        json.dumps(contract["promotion_rules"], sort_keys=True),
        payload.get("agent_name") or payload.get("source_agent") or rec.get("source_agent"),
        rec.get("recommendation_id"),
        now,
        now,
    )
    try:
        conn.execute(
            """
            insert into strategy_lab_experiments (
                strategy_lab_id, version, parent_strategy_lab_id, experiment_type, status, hypothesis,
                strategy_logic_json, original_strategy_logic_json, compile_status, compile_diagnostics_json,
                data_requirements_json, risk_gates_json,
                promotion_rules_json, source_agent, source_recommendation_id,
                created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
        created = True
    except sqlite3.IntegrityError:
        conn.execute(
            """
            update strategy_lab_experiments
            set updated_at = ?, status = case
                    when status in ('promoted_to_code', 'promotion_queued') then status
                    else ?
                end,
                hypothesis = ?,
                experiment_type = ?,
                strategy_logic_json = ?,
                original_strategy_logic_json = ?,
                compiled_strategy_logic_json = '{}',
                compile_status = 'uncompiled',
                compile_diagnostics_json = ?,
                data_requirements_json = ?,
                risk_gates_json = ?,
                promotion_rules_json = ?,
                source_recommendation_id = coalesce(source_recommendation_id, ?)
            where strategy_lab_id = ?
            """,
            (
                now,
                contract["status"],
                contract["hypothesis"],
                contract["experiment_type"],
                json.dumps(contract["strategy_logic"], sort_keys=True),
                json.dumps(contract["strategy_logic"], sort_keys=True),
                json.dumps(
                    {
                        "reason": (
                            "non_market_experiment_routed_outside_strategy_lab"
                            if contract["experiment_type"] != "market_strategy"
                            else "awaiting_runtime_contract_compilation"
                        )
                    },
                    sort_keys=True,
                ),
                json.dumps(contract["data_requirements"], sort_keys=True),
                json.dumps(contract["risk_gates"], sort_keys=True),
                json.dumps(contract["promotion_rules"], sort_keys=True),
                rec.get("recommendation_id"),
                contract["strategy_lab_id"],
            ),
        )
        conn.commit()
        created = False

    return [
        {
            "action_status": "created",
            "artifact": "strategy_lab_experiment",
            "strategy_lab_id": contract["strategy_lab_id"],
            "experiment_type": contract["experiment_type"],
            "status": contract["status"],
            "created": created,
        }
    ]


def _active_experiments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select *
        from strategy_lab_experiments
        where status in ('active_testing', 'needs_more_evidence')
          and experiment_type = 'market_strategy'
          and compile_status = 'compiled'
        order by updated_at desc
        limit 100
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        compiled_logic = item.pop("compiled_strategy_logic_json", None)
        fallback_logic = item.pop("strategy_logic_json", None)
        stored_logic = compiled_logic or fallback_logic
        item["strategy_logic"] = _json_loads(stored_logic, {})
        item.pop("original_strategy_logic_json", None)
        item["compile_diagnostics"] = _json_loads(item.pop("compile_diagnostics_json", None), {})
        item["strategy_logic"] = _normalize_strategy_logic(item["strategy_logic"])
        item["experiment_type"] = _resolved_experiment_type(
            item.get("experiment_type"),
            item,
            item["strategy_logic"],
        )
        item["data_requirements"] = _json_loads(item.pop("data_requirements_json"), {})
        item["risk_gates"] = _json_loads(item.pop("risk_gates_json"), {})
        item["promotion_rules"] = _json_loads(item.pop("promotion_rules_json"), {})
        item["evaluation"] = _json_loads(item.pop("evaluation_json"), {})
        output.append(item)
    return output


def _allowed(value: Any, allowed: list[str]) -> bool:
    if not allowed:
        return True
    value_text = str(value or "").strip().lower()
    return value_text in {str(item).strip().lower() for item in allowed}


def _direction_polarity(direction: Any) -> str | None:
    text = str(direction or "").lower()
    if text.startswith("long") or "_long_" in text or text.endswith("_long"):
        return "long"
    if text.startswith("short") or "_short_" in text or text.endswith("_short"):
        return "short"
    return None


def _direction_allowed(value: Any, allowed: list[str]) -> bool:
    if not allowed:
        return True
    value_text = str(value)
    allowed_set = set(allowed)
    if value_text in allowed_set:
        return True
    polarity = _direction_polarity(value_text)
    return bool(polarity and polarity in allowed_set)


def _candidate_edge(candidate: dict) -> float:
    if candidate.get("edge_bps_estimate") is not None:
        return _as_float(candidate.get("edge_bps_estimate"))
    funding = abs(_as_float(candidate.get("funding_bps")))
    basis = min(abs(_as_float(candidate.get("basis_bps"))) * 0.45, 30.0)
    return funding + basis


def _candidate_route_status(candidate: dict) -> str:
    return str(
        candidate.get("route_status")
        or (candidate.get("execution_route") or {}).get("route_status")
        or (candidate.get("execution_feasibility") or {}).get("status")
        or "unknown"
    ).lower()


def _paper_route_rank(candidate: dict) -> int:
    return {
        "standard": 0,
        "feasible": 0,
        "paper_proxy": 1,
        "conditional": 2,
    }.get(_candidate_route_status(candidate), 3)


def _candidate_asset_class(candidate: dict) -> str | None:
    raw = str(candidate.get("asset_class") or candidate.get("market_type") or "").strip()
    context = " ".join(
        str(candidate.get(key) or "")
        for key in ("asset_class", "market_type", "market_surface", "trade_type", "instrument_type")
    ).lower()
    if "crypto" in context or "perp" in context:
        return "crypto"
    if any(token in context for token in ("equity", "stock", "etf", "adr")):
        return "equity"
    if any(token in context for token in ("future", "commodity", "dairy")):
        return "futures"
    if "prediction" in context or "event" in context:
        return "prediction_market"
    return raw or None


def _candidate_region(candidate: dict) -> str | None:
    raw = str(candidate.get("region") or "").strip()
    if raw:
        return raw
    if _candidate_asset_class(candidate) in {"crypto", "prediction_market"}:
        return "global"
    return None


def _candidate_stale_minutes(candidate: dict) -> float | None:
    if candidate.get("stale_minutes") is not None:
        return _as_float(candidate.get("stale_minutes"))
    if candidate.get("freshness_age_seconds") is not None:
        return _as_float(candidate.get("freshness_age_seconds")) / 60.0
    for key in ("seen_at", "observed_at", "detected_at", "as_of", "updated_at", "timestamp"):
        observed = _parse_time(candidate.get(key))
        if not observed:
            continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - observed).total_seconds() / 60.0)
    return None


def _candidate_quality_score(candidate: dict) -> float | None:
    direct_values = (
        candidate.get("quality_score"),
        candidate.get("execution_quality_score"),
        candidate.get("instrument_quality_score"),
        (candidate.get("strategy_reliability") or {}).get("quality_score"),
    )
    for raw in direct_values:
        if raw is None:
            continue
        value = _as_float(raw)
        return value * 100.0 if 0.0 <= value <= 1.0 else value

    components: list[tuple[float, float]] = []
    if candidate.get("liquidity_score") is not None:
        liquidity = _as_float(candidate.get("liquidity_score"))
        liquidity = liquidity * 100.0 if 0.0 <= liquidity <= 1.0 else liquidity
        components.append((max(0.0, min(100.0, liquidity)), 0.55))
    if candidate.get("spread_bps") is not None:
        spread_quality = max(0.0, 100.0 - min(100.0, _as_float(candidate.get("spread_bps")) * 4.0))
        components.append((spread_quality, 0.3))
    stale = _candidate_stale_minutes(candidate)
    if stale is not None:
        freshness_quality = max(0.0, 100.0 - min(100.0, stale * 5.0))
        components.append((freshness_quality, 0.15))
    if not components:
        return None
    weight = sum(item[1] for item in components)
    return round(sum(value * item_weight for value, item_weight in components) / weight, 3)


def _candidate_field_value(candidate: dict, field: str) -> Any:
    if candidate.get(field) is not None:
        return candidate.get(field)
    aliases = {
        "edge_bps": ("edge_bps_estimate", "net_edge_bps_estimate", "gross_edge_bps_estimate"),
        "edge_bps_estimate": ("net_edge_bps_estimate", "edge_bps", "gross_edge_bps_estimate"),
        "quality": ("quality_score",),
        "liquidity": ("liquidity_score",),
        "spread": ("spread_bps",),
        "as_of": ("seen_at", "observed_at", "detected_at"),
        "detected_at": ("seen_at", "observed_at", "as_of"),
        "observed_at": ("seen_at", "detected_at", "as_of"),
        "seen_at": ("observed_at", "detected_at", "as_of"),
        "updated_at": ("seen_at", "observed_at", "detected_at", "as_of"),
        "timestamp": ("seen_at", "observed_at", "detected_at", "as_of", "updated_at"),
        "price": ("last", "mark_px", "index_px", "mid"),
    }
    for alias in aliases.get(field, ()):
        if candidate.get(alias) is not None:
            return candidate.get(alias)
    if field in {"quality", "quality_score"}:
        return _candidate_quality_score(candidate)
    if field == "stale_minutes":
        return _candidate_stale_minutes(candidate)
    if field == "freshness_age_seconds" and candidate.get("stale_minutes") is not None:
        return _as_float(candidate.get("stale_minutes")) * 60.0
    if field == "region":
        return _candidate_region(candidate)
    if field == "asset_class":
        return _candidate_asset_class(candidate)
    return None


def _scaled_gate(value: Any, candidate_value: Any, metric: str, default: float) -> float:
    threshold = _as_float(value, default)
    observed = _as_float(candidate_value, default)
    if metric in {"score", "quality"}:
        if 0.0 <= threshold <= 1.0 and observed > 1.5:
            return threshold * 100.0
        if threshold > 1.5 and 0.0 <= observed <= 1.0:
            return threshold / 100.0
    if metric == "liquidity":
        if threshold > 1.5 and 0.0 <= observed <= 1.0:
            return threshold / 100.0
        if 0.0 <= threshold <= 1.0 and observed > 1.5:
            return threshold * 100.0
    return threshold


def _matches_logic(candidate: dict, logic: dict, risk_gates: dict, settings: dict) -> tuple[bool, list[str]]:
    if candidate.get("strategy_lab_id"):
        return False, ["already_strategy_lab_candidate"]
    reasons = []
    allowed_trade_types = _as_list(logic.get("trade_types") or logic.get("source_trade_types"))
    allowed_venues = _as_list(logic.get("venues") or logic.get("allowed_venues"))
    allowed_directions = _as_list(logic.get("directions") or logic.get("allowed_directions"))
    allowed_regions = _as_list(logic.get("regions") or logic.get("allowed_regions"))
    allowed_asset_classes = _as_list(logic.get("asset_classes") or logic.get("allowed_asset_classes"))
    if not _has_strategy_scope(logic):
        reasons.append("missing_strategy_scope")
    if str(candidate.get("direction") or "").lower() == "watch_only" and not (
        bool(logic.get("allow_watch_only")) or "watch_only" in set(allowed_directions)
    ):
        reasons.append("watch_only_not_paper_testable")
    if not _allowed(candidate.get("trade_type"), allowed_trade_types):
        reasons.append("trade_type_not_allowed")
    if not _allowed(candidate.get("venue"), allowed_venues):
        reasons.append("venue_not_allowed")
    if not _direction_allowed(candidate.get("direction"), allowed_directions):
        reasons.append("direction_not_allowed")
    if not _allowed(_candidate_region(candidate), allowed_regions):
        reasons.append("region_not_allowed")
    if not _allowed(_candidate_asset_class(candidate), allowed_asset_classes):
        reasons.append("asset_class_not_allowed")

    risk = settings.get("risk", {})
    min_edge = _as_float(risk_gates.get("min_edge_bps", logic.get("min_edge_bps")), _as_float(risk.get("min_net_edge_bps"), 2.0))
    candidate_score = _candidate_field_value(candidate, "score")
    candidate_liquidity = _candidate_field_value(candidate, "liquidity_score")
    candidate_quality = _candidate_field_value(candidate, "quality_score")
    min_score = _scaled_gate(
        risk_gates.get("min_score", logic.get("min_score")), candidate_score, "score", 0.0
    )
    min_liquidity = _scaled_gate(
        risk_gates.get("min_liquidity_score", logic.get("min_liquidity_score")),
        candidate_liquidity,
        "liquidity",
        _as_float(risk.get("min_liquidity_score"), 0.35),
    )
    max_spread = _as_float(
        risk_gates.get("max_spread_bps", logic.get("max_spread_bps")),
        _as_float(risk.get("max_spread_bps"), 8.0),
    )
    min_quality = risk_gates.get("min_quality_score", logic.get("min_quality_score"))
    max_stale = risk_gates.get("max_stale_minutes", logic.get("max_stale_minutes"))
    if _candidate_edge(candidate) < min_edge:
        reasons.append("edge_below_gate")
    if _as_float(candidate_score) < min_score:
        reasons.append("score_below_gate")
    if _as_float(candidate_liquidity) < min_liquidity:
        reasons.append("liquidity_below_gate")
    if _as_float(candidate.get("spread_bps"), 999.0) > max_spread:
        reasons.append("spread_above_gate")
    if min_quality is not None:
        quality_gate = _scaled_gate(min_quality, candidate_quality, "quality", 0.0)
        if candidate_quality is None or _as_float(candidate_quality) < quality_gate:
            reasons.append("quality_below_gate")
    if max_stale is not None and _as_float(_candidate_field_value(candidate, "stale_minutes"), 0.0) > _as_float(max_stale):
        reasons.append("stale_above_gate")
    if bool(risk_gates.get("require_route_feasible") or logic.get("require_route_feasible")):
        route_status = str(
            candidate.get("route_status")
            or (candidate.get("execution_route") or {}).get("route_status")
            or (candidate.get("execution_feasibility") or {}).get("status")
            or "unknown"
        ).lower()
        if route_status not in {"standard", "conditional", "feasible", "paper_proxy"}:
            reasons.append(f"route_not_feasible:{route_status}")

    required_fields = _as_list(logic.get("required_fields") or risk_gates.get("required_fields"))
    missing = [field for field in required_fields if _candidate_field_value(candidate, field) is None]
    if missing:
        reasons.append("missing_required_fields:" + ",".join(missing[:5]))
    allowed_field_values = _first_dict(
        risk_gates.get("allowed_field_values"),
        logic.get("allowed_field_values"),
    )
    for field, allowed_values in allowed_field_values.items():
        observed = _candidate_field_value(candidate, str(field))
        if observed is None:
            reasons.append(f"missing_gate_field:{field}")
        elif not _allowed(observed, _as_list(allowed_values)):
            reasons.append(f"{field}_not_allowed")
    min_field_values = _first_dict(
        risk_gates.get("min_field_values"),
        logic.get("min_field_values"),
    )
    for field, threshold in min_field_values.items():
        observed = _candidate_field_value(candidate, str(field))
        if observed is None:
            reasons.append(f"missing_gate_field:{field}")
        elif _as_float(observed) < _as_float(threshold):
            reasons.append(f"{field}_below_gate")
    max_field_values = _first_dict(
        risk_gates.get("max_field_values"),
        logic.get("max_field_values"),
    )
    for field, threshold in max_field_values.items():
        observed = _candidate_field_value(candidate, str(field))
        if observed is None:
            reasons.append(f"missing_gate_field:{field}")
        elif _as_float(observed) > _as_float(threshold):
            reasons.append(f"{field}_above_gate")
    return not reasons, reasons


def _scope_match_reasons(candidate: dict, logic: dict) -> list[str]:
    """Check whether a live candidate can supply a contract, without applying alpha gates."""

    reasons: list[str] = []
    allowed_trade_types = _as_list(logic.get("trade_types") or logic.get("source_trade_types"))
    allowed_venues = _as_list(logic.get("venues") or logic.get("allowed_venues"))
    allowed_directions = _as_list(logic.get("directions") or logic.get("allowed_directions"))
    allowed_regions = _as_list(logic.get("regions") or logic.get("allowed_regions"))
    allowed_asset_classes = _as_list(logic.get("asset_classes") or logic.get("allowed_asset_classes"))
    if str(candidate.get("direction") or "").lower() == "watch_only" and not (
        bool(logic.get("allow_watch_only")) or "watch_only" in set(allowed_directions)
    ):
        reasons.append("watch_only_not_paper_testable")
    if not _allowed(candidate.get("trade_type"), allowed_trade_types):
        reasons.append("trade_type_not_observed")
    if not _allowed(candidate.get("venue"), allowed_venues):
        reasons.append("venue_not_observed")
    if not _direction_allowed(candidate.get("direction"), allowed_directions):
        reasons.append("direction_not_observed")
    if not _allowed(_candidate_region(candidate), allowed_regions):
        reasons.append("region_not_observed")
    if not _allowed(_candidate_asset_class(candidate), allowed_asset_classes):
        reasons.append("asset_class_not_observed")
    required_fields = _as_list(logic.get("required_fields"))
    missing = [field for field in required_fields if _candidate_field_value(candidate, field) is None]
    if missing:
        reasons.append("missing_required_fields:" + ",".join(missing[:8]))
    return reasons


def _runtime_contract_evidence(conn: sqlite3.Connection, current: list[dict], limit: int = 750) -> list[dict]:
    """Combine current candidates with bounded persisted runtime exemplars.

    A contract must not become invalid merely because its exchange is closed in
    the current minute. Recent opportunities and admission records describe
    what the running system can produce without turning memory into a new
    strategy language.
    """

    evidence = [dict(candidate) for candidate in current if isinstance(candidate, dict)]
    try:
        rows = conn.execute(
            """
            select candidate_json
            from opportunities
            where candidate_json is not null and candidate_json != ''
            order by id desc
            limit ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        for row in rows:
            candidate = _json_loads(row["candidate_json"], {})
            if isinstance(candidate, dict) and candidate and not candidate.get("strategy_lab_id"):
                evidence.append(candidate)
    except sqlite3.OperationalError:
        pass
    try:
        rows = conn.execute(
            """
            select venue, inst_id, market_surface, details_json
            from (
                select venue, inst_id, market_surface, details_json, last_seen_at,
                       row_number() over (
                           partition by venue, market_surface
                           order by last_seen_at desc
                       ) as recency_rank
                from market_admission_states
            )
            where recency_rank = 1
            order by last_seen_at desc
            limit ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        for row in rows:
            details = _json_loads(row["details_json"], {})
            if not isinstance(details, dict):
                details = {}
            evidence.append(
                {
                    **details,
                    "venue": row["venue"],
                    "inst_id": row["inst_id"],
                    "market_surface": row["market_surface"],
                    "strategy_lab_evidence_only": True,
                }
            )
    except sqlite3.OperationalError:
        pass

    deduped: list[dict] = []
    seen = set()
    for candidate in evidence:
        key = (
            str(candidate.get("venue") or ""),
            str(candidate.get("inst_id") or ""),
            str(candidate.get("trade_type") or ""),
            str(candidate.get("direction") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _scope_capability_issues(logic: dict, vocabulary: dict, candidates: list[dict]) -> list[str]:
    issues: list[str] = []
    observed_trade_types = {str(item).lower() for item in vocabulary.get("trade_types") or []}
    observed_venues = {str(item).lower() for item in vocabulary.get("venues") or []}
    observed_directions = {str(item).lower() for item in vocabulary.get("directions") or []}
    observed_regions = {str(item).lower() for item in vocabulary.get("regions") or []}
    observed_assets = {str(item).lower() for item in vocabulary.get("asset_classes") or []}

    trade_types = {item.lower() for item in _as_list(logic.get("trade_types") or logic.get("source_trade_types"))}
    venues = {item.lower() for item in _as_list(logic.get("venues") or logic.get("allowed_venues"))}
    directions = {item.lower() for item in _as_list(logic.get("directions") or logic.get("allowed_directions"))}
    regions = {item.lower() for item in _as_list(logic.get("regions") or logic.get("allowed_regions"))}
    asset_classes = {
        item.lower() for item in _as_list(logic.get("asset_classes") or logic.get("allowed_asset_classes"))
    }
    if trade_types and not trade_types.intersection(observed_trade_types | {item.lower() for item in FALLBACK_TRADE_TYPE_EXAMPLES}):
        issues.append("trade_type_unavailable")
    if venues and not venues.intersection(observed_venues):
        issues.append("venue_unavailable")
    known_directions = observed_directions | {item.lower() for item in FALLBACK_DIRECTION_EXAMPLES} | GENERIC_DIRECTIONS
    if directions and not directions.intersection(known_directions):
        issues.append("direction_unavailable")
    if regions and not regions.intersection(observed_regions | {"global"}):
        issues.append("region_unavailable")
    if asset_classes and not asset_classes.intersection(observed_assets):
        issues.append("asset_class_unavailable")

    required_fields = _as_list(logic.get("required_fields"))
    unsupported = [
        field
        for field in required_fields
        if not any(_candidate_field_value(candidate, field) is not None for candidate in candidates)
    ]
    if unsupported:
        issues.append("unsupported_required_fields:" + ",".join(unsupported))
    return issues


def _compile_strategy_lab_contracts(
    conn: sqlite3.Connection,
    candidates: list[dict],
) -> dict:
    """Compile model intent against the actual runtime candidate schema.

    Compilation is deliberately deterministic. The original model contract is
    retained for audit. A contract may compile from persisted runtime evidence
    while its market is closed, but only a current live candidate may enter the
    paper candidate generator.
    """

    runtime_evidence = _runtime_contract_evidence(conn, candidates)
    vocabulary = _runtime_strategy_vocabulary(runtime_evidence)
    schema_payload = {
        "fields": sorted(vocabulary.get("candidate_fields") or []),
        "trade_types": sorted(vocabulary.get("trade_types") or []),
        "directions": sorted(vocabulary.get("directions") or []),
        "venues": sorted(vocabulary.get("venues") or []),
    }
    schema_fingerprint = hashlib.sha256(
        json.dumps(schema_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    rows = conn.execute(
        """
        select *
        from strategy_lab_experiments
        where experiment_type = 'market_strategy'
          and status in ('proposed', 'active_testing', 'needs_data', 'needs_route', 'needs_more_evidence')
        order by updated_at desc
        limit 500
        """
    ).fetchall()
    summary = Counter()
    diagnostics: dict[str, dict] = {}
    now = _utc()
    for raw in rows:
        row = dict(raw)
        original = _json_loads(row.get("original_strategy_logic_json"), {})
        if not original:
            original = _json_loads(row.get("strategy_logic_json"), {})
        data_requirements = _json_loads(row.get("data_requirements_json"), {})
        logic = _normalize_strategy_logic(original, vocabulary)

        # Admission-generated ideas carry an exact instrument as evidence. Use
        # that observation to learn the runtime surface, but keep the strategy
        # scoped to the venue/surface rather than hard-coding one instrument.
        requested_inst = str(data_requirements.get("inst_id") or "").strip()
        evidence_candidates = [
            candidate
            for candidate in runtime_evidence
            if requested_inst and str(candidate.get("inst_id")) == requested_inst
        ]
        if evidence_candidates:
            exemplar = evidence_candidates[0]
            if not _as_list(logic.get("venues") or logic.get("allowed_venues")) and exemplar.get("venue"):
                logic["venues"] = [str(exemplar["venue"])]
            if not _as_list(logic.get("trade_types") or logic.get("source_trade_types")) and exemplar.get("trade_type"):
                logic["trade_types"] = [str(exemplar["trade_type"])]
            logic.setdefault("normalization_notes", []).append("compiled_scope_from_admission_exemplar")
            logic["normalization_notes"] = _unique(_as_list(logic.get("normalization_notes")))

        nearest: list[dict] = []
        matches: list[dict] = []
        if _has_strategy_scope(logic):
            for candidate in runtime_evidence:
                reasons = _scope_match_reasons(candidate, logic)
                if not reasons:
                    matches.append(candidate)
                else:
                    nearest.append(
                        {
                            "inst_id": candidate.get("inst_id"),
                            "venue": candidate.get("venue"),
                            "trade_type": candidate.get("trade_type"),
                            "direction": candidate.get("direction"),
                            "failed_gate_count": len(reasons),
                            "failed_gates": reasons[:5],
                        }
                    )

        required_fields = _as_list(logic.get("required_fields"))
        unsupported_fields = [
            field
            for field in required_fields
            if not any(_candidate_field_value(candidate, field) is not None for candidate in runtime_evidence)
        ]
        capability_issues = _scope_capability_issues(logic, vocabulary, runtime_evidence)
        if not _has_strategy_scope(logic):
            compile_status = "needs_contract_repair"
            status = "needs_data"
            reason = "missing_strategy_scope"
        elif unsupported_fields:
            compile_status = "needs_data"
            status = "needs_data"
            reason = "unsupported_required_fields"
        elif not runtime_evidence:
            compile_status = "needs_data"
            status = "needs_data"
            reason = "runtime_candidate_pool_empty"
        elif capability_issues:
            compile_status = "needs_data"
            status = "needs_data"
            reason = "runtime_capability_unavailable"
        else:
            compile_status = "compiled"
            status = "needs_more_evidence" if row.get("status") == "needs_more_evidence" else "active_testing"
            reason = "compiled_against_runtime_schema" if matches else "compiled_dormant_scope"

        diagnostic = {
            "compiled_at": now,
            "compile_status": compile_status,
            "reason": reason,
            "runtime_schema_fingerprint": schema_fingerprint,
            "source_candidate_count": len(runtime_evidence),
            "current_candidate_count": len(candidates),
            "persisted_evidence_count": max(0, len(runtime_evidence) - len(candidates)),
            "scope_match_count": len(matches),
            "unsupported_required_fields": unsupported_fields,
            "capability_issues": capability_issues,
            "match_preview": [
                {
                    "inst_id": item.get("inst_id"),
                    "venue": item.get("venue"),
                    "trade_type": item.get("trade_type"),
                    "direction": item.get("direction"),
                }
                for item in matches[:5]
            ],
            "nearest_candidates": sorted(
                nearest,
                key=lambda item: (int(item.get("failed_gate_count") or 999), str(item.get("inst_id") or "")),
            )[:5],
            "llm_repair_requested": compile_status == "needs_contract_repair" and int(row.get("compile_attempts") or 0) == 0,
        }
        prior_evaluation = _json_loads(row.get("evaluation_json"), {})
        conn.execute(
            """
            update strategy_lab_experiments
            set original_strategy_logic_json = case
                    when original_strategy_logic_json is null or original_strategy_logic_json = '{}'
                    then ? else original_strategy_logic_json end,
                strategy_logic_json = ?, compiled_strategy_logic_json = ?, compile_status = ?,
                compile_diagnostics_json = ?, runtime_schema_fingerprint = ?,
                compile_attempts = compile_attempts + 1, last_compiled_at = ?,
                status = ?, updated_at = ?, evaluation_json = ?
            where strategy_lab_id = ?
            """,
            (
                json.dumps(original, sort_keys=True),
                json.dumps(logic, sort_keys=True),
                json.dumps(logic, sort_keys=True) if compile_status == "compiled" else "{}",
                compile_status,
                json.dumps(diagnostic, sort_keys=True),
                schema_fingerprint,
                now,
                status,
                now,
                json.dumps({**prior_evaluation, "contract_compilation": diagnostic}, sort_keys=True),
                row["strategy_lab_id"],
            ),
        )
        summary[compile_status] += 1
        diagnostics[str(row["strategy_lab_id"])] = diagnostic
    conn.commit()
    return {
        "runtime_schema_fingerprint": schema_fingerprint,
        "by_compile_status": dict(summary),
        "diagnostics": diagnostics,
    }


def generate_strategy_lab_candidates(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    price_observations: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("enabled", True):
        return [], {"enabled": False, "generated_candidates": 0}

    compilation = _compile_strategy_lab_contracts(conn, candidates)
    experiments = _active_experiments(conn)
    max_total = int(cfg.get("max_candidates_per_loop", 25))
    max_per_experiment = int(cfg.get("max_candidates_per_experiment", 5))
    default_bonus = float(cfg.get("candidate_score_bonus", 1.0))
    max_bonus = float(cfg.get("max_candidate_score_bonus", 5.0))
    generated = []
    per_experiment: dict[str, int] = Counter()
    rejects: dict[str, Counter] = defaultdict(Counter)
    nearest_candidates: dict[str, list[dict]] = defaultdict(list)
    status_by_experiment: dict[str, str] = {}

    pool = sorted(candidates, key=lambda row: (_paper_route_rank(row), -_as_float(row.get("score"))))
    runtime_vocabulary = _runtime_strategy_vocabulary(pool)
    for experiment_id, diagnostic in (compilation.get("diagnostics") or {}).items():
        if diagnostic.get("compile_status") == "compiled":
            continue
        status_by_experiment[experiment_id] = "needs_data"
        rejects[experiment_id][str(diagnostic.get("reason") or "contract_not_compiled")] += 1
        for item in diagnostic.get("nearest_candidates") or []:
            nearest_candidates[experiment_id].append(dict(item))
            for reason in item.get("failed_gates") or []:
                rejects[experiment_id][str(reason)] += 1
    for experiment in experiments:
        if len(generated) >= max_total:
            break
        logic = _normalize_strategy_logic(experiment.get("strategy_logic") or {}, runtime_vocabulary)
        risk_gates = experiment.get("risk_gates") or {}
        bonus = max(0.0, min(max_bonus, _as_float(logic.get("score_bonus"), default_bonus)))
        edge_bonus = max(0.0, min(max_bonus, _as_float(logic.get("edge_bonus_bps"), 0.0)))
        local_limit = min(max_per_experiment, max(1, _as_int(logic.get("max_candidates_per_loop"), max_per_experiment)))
        for candidate in pool:
            if len(generated) >= max_total or per_experiment[experiment["strategy_lab_id"]] >= local_limit:
                break
            ok, reasons = _matches_logic(candidate, logic, risk_gates, settings)
            if not ok:
                for reason in reasons[:3]:
                    rejects[experiment["strategy_lab_id"]][reason] += 1
                nearest_candidates[experiment["strategy_lab_id"]].append(
                    {
                        "inst_id": candidate.get("inst_id"),
                        "venue": candidate.get("venue"),
                        "trade_type": candidate.get("trade_type"),
                        "direction": candidate.get("direction"),
                        "score": candidate.get("score"),
                        "failed_gate_count": len(reasons),
                        "failed_gates": reasons[:5],
                    }
                )
                continue
            lab_candidate = dict(candidate)
            lab_candidate["strategy_lab_id"] = experiment["strategy_lab_id"]
            lab_candidate["strategy_lab_version"] = int(experiment["version"])
            lab_candidate["strategy_lab_experiment_type"] = experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE)
            lab_candidate["strategy_lab_hypothesis"] = experiment["hypothesis"]
            lab_candidate["strategy_lab_logic_type"] = logic.get("type", "candidate_filter")
            lab_candidate["strategy_lab_source_trade_type"] = candidate.get("trade_type")
            lab_candidate["strategy_lab_source_signal_key"] = candidate.get("signal_key")
            lab_candidate["strategy_lab_candidate"] = True
            lab_candidate["strategy_lab_normalized_features"] = {
                "quality_score": _candidate_field_value(candidate, "quality_score"),
                "stale_minutes": _candidate_field_value(candidate, "stale_minutes"),
                "region": _candidate_region(candidate),
                "asset_class": _candidate_asset_class(candidate),
            }
            lab_candidate["score"] = round(min(100.0, _as_float(candidate.get("score")) + bonus), 3)
            lab_candidate["edge_bps_estimate"] = round(max(0.0, _candidate_edge(candidate) + edge_bonus), 3)
            lab_candidate["thesis"] = (
                f"Strategy Lab {experiment['strategy_lab_id']}: {experiment['hypothesis']}"
            )[:1000]
            generated.append(lab_candidate)
            per_experiment[experiment["strategy_lab_id"]] += 1

        experiment_id = experiment["strategy_lab_id"]
        reason_counts = rejects.get(experiment_id, Counter())
        if per_experiment[experiment_id] > 0:
            diagnostic_status = "active_testing"
        elif not pool:
            diagnostic_status = "needs_data"
        elif any(str(reason).startswith("route_not_feasible") for reason in reason_counts):
            diagnostic_status = "needs_route"
        elif (compilation.get("diagnostics") or {}).get(experiment_id, {}).get("compile_status") == "compiled":
            diagnostic_status = "needs_more_evidence"
        elif reason_counts and all(
            str(reason).startswith(
                (
                    "trade_type_not_allowed",
                    "venue_not_allowed",
                    "direction_not_allowed",
                    "region_not_allowed",
                    "asset_class_not_allowed",
                    "missing_required_fields",
                )
            )
            for reason in reason_counts
        ):
            diagnostic_status = "needs_data"
        else:
            diagnostic_status = "needs_more_evidence"
        status_by_experiment[experiment_id] = diagnostic_status
        nearest = sorted(
            nearest_candidates.get(experiment_id, []),
            key=lambda row: (int(row.get("failed_gate_count") or 999), -_as_float(row.get("score"))),
        )[:5]
        generation_diagnostic = {
            "checked_at": _utc(),
            "status": diagnostic_status,
            "source_candidate_count": len(pool),
            "generated_candidate_count": int(per_experiment[experiment_id]),
            "contract_compile_status": (compilation.get("diagnostics") or {}).get(experiment_id, {}).get(
                "compile_status"
            ),
            "dominant_reject_reasons": dict(reason_counts.most_common(8)),
            "nearest_candidates": nearest,
            "runtime_vocabulary": {
                "trade_types": sorted(runtime_vocabulary.get("trade_types") or []),
                "directions": sorted(runtime_vocabulary.get("directions") or []),
            },
        }
        prior_evaluation = experiment.get("evaluation") or {}
        conn.execute(
            """
            update strategy_lab_experiments
            set status = ?, updated_at = ?, last_evaluated_at = ?, evaluation_json = ?
            where strategy_lab_id = ?
            """,
            (
                diagnostic_status,
                _utc(),
                _utc(),
                json.dumps({**prior_evaluation, "generation_diagnostic": generation_diagnostic}, sort_keys=True),
                experiment_id,
            ),
        )
    conn.commit()

    report = {
        "enabled": True,
        "generated_at": _utc(),
        "active_experiments": len(experiments),
        "source_candidate_count": len(candidates),
        "price_observation_count": len(price_observations or []),
        "generated_candidates": len(generated),
        "generated_by_experiment": dict(per_experiment),
        "generated_by_experiment_type": dict(
            Counter(item.get("strategy_lab_experiment_type", DEFAULT_EXPERIMENT_TYPE) for item in generated)
        ),
        "reject_reasons_by_experiment": {key: dict(value) for key, value in rejects.items()},
        "status_by_experiment": status_by_experiment,
        "nearest_candidates_by_experiment": {
            key: sorted(
                rows,
                key=lambda row: (int(row.get("failed_gate_count") or 999), -_as_float(row.get("score"))),
            )[:5]
            for key, rows in nearest_candidates.items()
        },
        "contract_compilation": compilation,
    }
    return generated, report


def _pnl_stats(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "avg_pnl_bps": None,
            "win_rate": None,
            "worst_decile_pnl_bps": None,
            "min_pnl_bps": None,
            "max_pnl_bps": None,
        }
    ordered = sorted(values)
    decile_n = max(1, int(math.ceil(len(ordered) * 0.1)))
    worst_decile = ordered[:decile_n]
    wins = sum(1 for value in values if value > 0)
    return {
        "count": len(values),
        "avg_pnl_bps": round(sum(values) / len(values), 3),
        "win_rate": round(wins / len(values), 3),
        "worst_decile_pnl_bps": round(sum(worst_decile) / len(worst_decile), 3),
        "min_pnl_bps": round(min(values), 3),
        "max_pnl_bps": round(max(values), 3),
    }


def _experiment_outcomes(conn: sqlite3.Connection, strategy_lab_id: str, horizon: int) -> dict:
    rows = conn.execute(
        """
        select p.id, p.opened_at, p.closed_at, p.venue, p.inst_id, p.direction, p.trade_type,
               p.candidate_json, p.review_json, o.pnl_bps, o.measurement_status
        from paper_trades p
        left join paper_trade_outcomes o
          on o.trade_id = p.id and o.horizon_minutes = ?
        where p.strategy_lab_id = ?
        """,
        (int(horizon), strategy_lab_id),
    ).fetchall()
    valid = []
    status_counts: Counter = Counter()
    route_counts: Counter = Counter()
    by_region: dict[str, list[float]] = defaultdict(list)
    by_venue: dict[str, list[float]] = defaultdict(list)
    examples = []
    for row in rows:
        item = dict(row)
        status = str(item.get("measurement_status") or "missing")
        status_counts[status] += 1
        candidate = _json_loads(item.get("candidate_json"), {})
        review = _json_loads(item.get("review_json"), {})
        route = str(review.get("route_status") or candidate.get("route_status") or (candidate.get("execution_feasibility") or {}).get("status") or "unknown")
        route_counts[route] += 1
        if status == "valid" and item.get("pnl_bps") is not None:
            pnl = _as_float(item["pnl_bps"])
            valid.append(pnl)
            by_region[str(candidate.get("region") or "unknown")].append(pnl)
            by_venue[str(item.get("venue") or "unknown")].append(pnl)
            if len(examples) < 10:
                examples.append(
                    {
                        "trade_id": item["id"],
                        "venue": item["venue"],
                        "inst_id": item["inst_id"],
                        "direction": item["direction"],
                        "pnl_bps": round(pnl, 3),
                    }
                )
    return {
        "trade_count": len(rows),
        "valid_count": len(valid),
        "label_status_counts": dict(status_counts),
        "route_status_counts": dict(route_counts),
        "valid_label_rate": round(len(valid) / len(rows), 3) if rows else 0.0,
        "metrics": _pnl_stats(valid),
        "by_region": {key: _pnl_stats(value) for key, value in by_region.items()},
        "by_venue": {key: _pnl_stats(value) for key, value in by_venue.items()},
        "examples": examples,
    }


def _rules(settings: dict, experiment: dict) -> dict:
    defaults = settings.get("strategy_lab", {})
    custom = experiment.get("promotion_rules") or {}
    return {
        "horizon_minutes": int(custom.get("horizon_minutes", defaults.get("evaluation_horizon_minutes", 60))),
        "expand_min_labels": int(custom.get("expand_min_labels", defaults.get("expand_min_labels", 12))),
        "expand_min_avg_pnl_bps": float(custom.get("expand_min_avg_pnl_bps", defaults.get("expand_min_avg_pnl_bps", 6.0))),
        "expand_min_win_rate": float(custom.get("expand_min_win_rate", defaults.get("expand_min_win_rate", 0.52))),
        "promote_min_labels": int(custom.get("promote_min_labels", defaults.get("promote_min_labels", 30))),
        "promote_min_active_hours": float(custom.get("promote_min_active_hours", defaults.get("promote_min_active_hours", 48.0))),
        "promote_min_avg_pnl_bps": float(custom.get("promote_min_avg_pnl_bps", defaults.get("promote_min_avg_pnl_bps", 10.0))),
        "promote_min_win_rate": float(custom.get("promote_min_win_rate", defaults.get("promote_min_win_rate", 0.53))),
        "promote_min_valid_label_rate": float(custom.get("promote_min_valid_label_rate", defaults.get("promote_min_valid_label_rate", 0.90))),
        "promote_worst_decile_floor_bps": float(custom.get("promote_worst_decile_floor_bps", defaults.get("promote_worst_decile_floor_bps", -45.0))),
        "retire_min_labels": int(custom.get("retire_min_labels", defaults.get("retire_min_labels", 20))),
        "retire_max_avg_pnl_bps": float(custom.get("retire_max_avg_pnl_bps", defaults.get("retire_max_avg_pnl_bps", -8.0))),
        "retire_max_win_rate": float(custom.get("retire_max_win_rate", defaults.get("retire_max_win_rate", 0.43))),
        "consecutive_passes_to_promote": int(custom.get("consecutive_passes_to_promote", defaults.get("consecutive_passes_to_promote", 2))),
    }


def _queue_promotion(conn: sqlite3.Connection, experiment: dict, evaluation: dict, rules: dict) -> str | None:
    proposal_id = "strategy_lab_promotion_" + hashlib.sha256(
        f"{experiment['strategy_lab_id']}:{experiment['version']}".encode("utf-8")
    ).hexdigest()[:16]
    payload = {
        "action": "propose_code_change",
        "priority": 92,
        "title": f"Promote Strategy Lab {experiment['strategy_lab_id']} to coded strategy",
        "rationale": "Strategy Lab experiment passed deterministic paper-evidence promotion gates.",
        "market_key": f"strategy_lab.{experiment['strategy_lab_id']}",
        "signal_key": f"STRATEGY_LAB|{experiment['strategy_lab_id']}",
        "evidence": {
            "strategy_lab_id": experiment["strategy_lab_id"],
            "experiment_type": experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
            "evaluation": evaluation,
            "promotion_rules": rules,
        },
        "proposed_change": {
            "summary": "Turn the proven Strategy Lab contract into a stable paper-only scanner/strategy implementation.",
            "strategy_contract": {
                "strategy_lab_id": experiment["strategy_lab_id"],
                "version": experiment["version"],
                "experiment_type": experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
                "hypothesis": experiment["hypothesis"],
                "strategy_logic": experiment.get("strategy_logic", {}),
                "risk_gates": experiment.get("risk_gates", {}),
            },
            "required_candidate_fields": [
                "strategy_lab_id",
                "edge_bps_estimate",
                "score",
                "liquidity_score",
                "spread_bps",
                "last",
                "venue",
                "inst_id",
                "direction",
                "trade_type",
            ],
        },
        "change_category": "strategy_lab_promotion",
        "implementation_mode": "runtime_active",
        "code_change": {
            "change_category": "strategy_lab_promotion",
            "implementation_mode": "runtime_active",
            "expected_files": [
                "src/strategy_lab.py",
                "src/radar_loop.py",
                "tests/test_strategy_lab.py",
            ],
            "tests_to_run": ["python -m unittest tests.test_strategy_lab"],
            "rollback_criteria": "Revert if promoted strategy candidates fail validation, reports stop refreshing, or paper-only safety checks fail.",
            "evidence": {
                "strategy_lab_id": experiment["strategy_lab_id"],
                "experiment_type": experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
                "evaluation": evaluation,
            },
        },
    }
    rec_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    created = add_llm_recommendation(
        conn,
        rec_id,
        "propose_code_change",
        payload["title"],
        payload["rationale"],
        payload,
    )
    return rec_id if created else None


def _maybe_split_children(conn: sqlite3.Connection, experiment: dict, outcomes: dict) -> list[str]:
    created = []
    good_regions = [
        (region, metrics)
        for region, metrics in outcomes.get("by_region", {}).items()
        if metrics.get("count", 0) >= 12
        and (metrics.get("avg_pnl_bps") or 0.0) >= 10.0
        and (metrics.get("win_rate") or 0.0) >= 0.53
    ]
    bad_regions = [
        (region, metrics)
        for region, metrics in outcomes.get("by_region", {}).items()
        if metrics.get("count", 0) >= 12
        and ((metrics.get("avg_pnl_bps") or 0.0) <= -8.0 or (metrics.get("win_rate") or 1.0) <= 0.43)
    ]
    if not good_regions or not bad_regions:
        return created
    base_logic = dict(experiment.get("strategy_logic") or {})
    for region, _metrics in good_regions[:3]:
        child_id = f"{experiment['strategy_lab_id']}_{_slug(region, 'region')}_child"
        child_logic = dict(base_logic)
        child_logic["regions"] = [region]
        now = _utc()
        try:
            conn.execute(
                """
                insert into strategy_lab_experiments (
                    strategy_lab_id, version, parent_strategy_lab_id, experiment_type, status, hypothesis,
                    strategy_logic_json, data_requirements_json, risk_gates_json,
                    promotion_rules_json, source_agent, source_recommendation_id,
                    created_at, updated_at
                ) values (?, ?, ?, ?, 'active_testing', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    1,
                    experiment["strategy_lab_id"],
                    experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
                    f"{experiment['hypothesis']} narrowed to region {region}",
                    json.dumps(child_logic, sort_keys=True),
                    json.dumps(experiment.get("data_requirements") or {}, sort_keys=True),
                    json.dumps(experiment.get("risk_gates") or {}, sort_keys=True),
                    json.dumps(experiment.get("promotion_rules") or {}, sort_keys=True),
                    "strategy_lab_evaluator",
                    experiment.get("source_recommendation_id"),
                    now,
                    now,
                ),
            )
            created.append(child_id)
        except sqlite3.IntegrityError:
            pass
    if created:
        conn.execute(
            """
            update strategy_lab_experiments
            set status = 'split_into_children', updated_at = ?
            where strategy_lab_id = ?
            """,
            (_utc(), experiment["strategy_lab_id"]),
        )
        conn.commit()
    return created


def evaluate_strategy_lab(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("enabled", True):
        return {"enabled": False}
    experiments = _active_experiments(conn)
    evaluations = []
    for experiment in experiments:
        rules = _rules(settings, experiment)
        outcomes = _experiment_outcomes(conn, experiment["strategy_lab_id"], rules["horizon_minutes"])
        metrics = outcomes["metrics"]
        count = int(metrics.get("count") or 0)
        avg = metrics.get("avg_pnl_bps")
        win_rate = metrics.get("win_rate")
        worst_decile = metrics.get("worst_decile_pnl_bps")
        active_hours = _age_hours(experiment.get("created_at"))
        blocked_routes = int(outcomes.get("route_status_counts", {}).get("blocked", 0))
        generation_diagnostic = (experiment.get("evaluation") or {}).get("generation_diagnostic") or {}
        diagnostic_status = str(generation_diagnostic.get("status") or experiment.get("status") or "active_testing")
        decision = diagnostic_status if diagnostic_status in {"needs_data", "needs_route", "needs_more_evidence"} else "needs_more_evidence"
        status = diagnostic_status if diagnostic_status in {"needs_data", "needs_route", "needs_more_evidence"} else "active_testing"
        passes = int(experiment.get("consecutive_passes") or 0)
        promotion_id = None
        children = []

        promote_ready = (
            count >= rules["promote_min_labels"]
            and active_hours >= rules["promote_min_active_hours"]
            and (avg is not None and avg >= rules["promote_min_avg_pnl_bps"])
            and (win_rate is not None and win_rate >= rules["promote_min_win_rate"])
            and (worst_decile is not None and worst_decile > rules["promote_worst_decile_floor_bps"])
            and outcomes["valid_label_rate"] >= rules["promote_min_valid_label_rate"]
            and blocked_routes == 0
        )
        retire_ready = (
            count >= rules["retire_min_labels"]
            and (
                (avg is not None and avg <= rules["retire_max_avg_pnl_bps"])
                or (win_rate is not None and win_rate <= rules["retire_max_win_rate"])
            )
        )
        expand_ready = (
            count >= rules["expand_min_labels"]
            and (avg is not None and avg > rules["expand_min_avg_pnl_bps"])
            and (win_rate is not None and win_rate >= rules["expand_min_win_rate"])
            and (worst_decile is None or worst_decile > rules["promote_worst_decile_floor_bps"])
        )

        if promote_ready:
            passes += 1
            decision = "promotion_gate_passed"
            if passes >= rules["consecutive_passes_to_promote"]:
                promotion_id = _queue_promotion(conn, experiment, outcomes, rules)
                status = "promotion_queued"
                decision = "promotion_queued" if promotion_id else "promotion_already_queued"
        elif retire_ready:
            status = "retired_bad_evidence"
            passes = 0
            decision = "retired_bad_evidence"
        elif expand_ready:
            decision = "expand_testing_modestly"
        elif count >= rules["retire_min_labels"]:
            children = _maybe_split_children(conn, experiment, outcomes)
            if children:
                status = "split_into_children"
                decision = "split_into_children"
            else:
                decision = "mixed_keep_testing"
            passes = 0
        else:
            passes = 0

        evaluation = {
            "checked_at": _utc(),
            "decision": decision,
            "rules": rules,
            "outcomes": outcomes,
            "active_hours": round(active_hours, 3),
            "consecutive_passes": passes,
            "promotion_recommendation_id": promotion_id,
            "child_strategy_lab_ids": children,
            "generation_diagnostic": generation_diagnostic,
        }
        conn.execute(
            """
            update strategy_lab_experiments
            set status = ?, updated_at = ?, last_evaluated_at = ?,
                evaluation_json = ?, consecutive_passes = ?,
                promoted_proposal_id = coalesce(?, promoted_proposal_id)
            where strategy_lab_id = ?
            """,
            (
                status,
                _utc(),
                _utc(),
                json.dumps(evaluation, sort_keys=True),
                passes,
                promotion_id,
                experiment["strategy_lab_id"],
            ),
        )
        conn.commit()
        evaluations.append(
            {
                "strategy_lab_id": experiment["strategy_lab_id"],
                "experiment_type": experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
                "status": status,
                "decision": decision,
                "metrics": metrics,
                "valid_label_rate": outcomes["valid_label_rate"],
                "promotion_recommendation_id": promotion_id,
            }
        )
    return {"enabled": True, "evaluated": evaluations}


def strategy_lab_summary(conn: sqlite3.Connection, limit: int = 20) -> dict:
    _backfill_experiment_types(conn)
    rows = conn.execute(
        """
        select strategy_lab_id, version, parent_strategy_lab_id, status, hypothesis,
               experiment_type, strategy_logic_json, created_at, updated_at, last_evaluated_at, evaluation_json,
               consecutive_passes, promoted_proposal_id, compile_status,
               compile_diagnostics_json, runtime_schema_fingerprint, compile_attempts, last_compiled_at
        from strategy_lab_experiments
        order by updated_at desc
        limit ?
        """,
        (int(limit),),
    ).fetchall()
    items = []
    recent_status_counts: Counter = Counter()
    for row in rows:
        item = dict(row)
        logic = _json_loads(item.pop("strategy_logic_json"), {})
        item["experiment_type"] = _resolved_experiment_type(item.get("experiment_type"), item, logic)
        item["evaluation"] = _json_loads(item.pop("evaluation_json"), {})
        item["compile_diagnostics"] = _json_loads(item.pop("compile_diagnostics_json"), {})
        recent_status_counts[item["status"]] += 1
        items.append(item)
    total = conn.execute("select count(*) as n from strategy_lab_experiments").fetchone()
    status_counts = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            """
            select status, count(*) as n
            from strategy_lab_experiments
            group by status
            order by n desc
            """
        ).fetchall()
    }
    type_counts = {
        (row["experiment_type"] or DEFAULT_EXPERIMENT_TYPE): int(row["n"])
        for row in conn.execute(
            """
            select experiment_type, count(*) as n
            from strategy_lab_experiments
            group by experiment_type
            order by n desc
            """
        ).fetchall()
    }
    compile_status_counts = {
        (row["compile_status"] or "uncompiled"): int(row["n"])
        for row in conn.execute(
            """
            select compile_status, count(*) as n
            from strategy_lab_experiments
            where experiment_type = 'market_strategy'
            group by compile_status
            order by n desc
            """
        ).fetchall()
    }
    generated_candidates = 0
    if REPORT_JSON.exists():
        latest = _json_loads(REPORT_JSON.read_text(encoding="utf-8"), {})
        generated_candidates = int((latest.get("generation") or {}).get("generated_candidates") or 0)
    return {
        "enabled": True,
        "total_experiments": int(total["n"] if total else 0),
        "status_counts": dict(status_counts),
        "recent_status_counts": dict(recent_status_counts),
        "by_experiment_type": dict(type_counts),
        "compile_status_counts": compile_status_counts,
        "contract_repair_queue": [
            {
                "strategy_lab_id": item["strategy_lab_id"],
                "status": item["status"],
                "compile_status": item.get("compile_status"),
                "hypothesis": item.get("hypothesis"),
                "reason": (item.get("compile_diagnostics") or {}).get("reason"),
                "unsupported_required_fields": (item.get("compile_diagnostics") or {}).get(
                    "unsupported_required_fields", []
                ),
                "nearest_candidates": (item.get("compile_diagnostics") or {}).get("nearest_candidates", []),
            }
            for item in items
            if item.get("experiment_type") == "market_strategy" and item.get("compile_status") != "compiled"
        ][:20],
        "recent": items,
        "recent_market_strategies": [
            item for item in items if item.get("experiment_type") == "market_strategy"
        ],
        "recent_non_market_experiments": [
            item for item in items if item.get("experiment_type") != "market_strategy"
        ],
        "generated_candidates_last_cycle": generated_candidates,
        "report": str(REPORT_MD),
    }


def _backfill_experiment_types(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        select strategy_lab_id, experiment_type, status, hypothesis, strategy_logic_json
        from strategy_lab_experiments
        """
    ).fetchall()
    updates: list[tuple[str, str]] = []
    for row in rows:
        item = dict(row)
        logic = _json_loads(item.pop("strategy_logic_json"), {})
        resolved = _resolved_experiment_type(item.get("experiment_type"), item, logic)
        if resolved != _normalize_experiment_type(item.get("experiment_type")):
            updates.append((resolved, item["strategy_lab_id"]))
    if updates:
        conn.executemany(
            """
            update strategy_lab_experiments
            set experiment_type = ?
            where strategy_lab_id = ?
            """,
            updates,
        )
        conn.commit()
    return len(updates)


def write_strategy_lab_reports(conn: sqlite3.Connection, generation: dict | None = None, evaluation: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    backfilled_experiment_type_count = _backfill_experiment_types(conn)
    summary = strategy_lab_summary(conn)
    report = {
        "generated_at": _utc(),
        "summary": summary,
        "generation": generation or {},
        "evaluation": evaluation or {},
        "backfilled_experiment_type_count": backfilled_experiment_type_count,
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Strategy Lab Report",
        "",
        "Paper-only R&D strategies invented by the LLM swarm and tested through the normal radar loop.",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Total experiments: `{summary.get('total_experiments', 0)}`",
        f"- Status counts: `{summary.get('status_counts', {})}`",
        f"- Experiment types: `{summary.get('by_experiment_type', {})}`",
        f"- Runtime contract compilation: `{summary.get('compile_status_counts', {})}`",
        f"- Candidates generated this loop: `{(generation or {}).get('generated_candidates', 0)}`",
        "",
        "## Market Strategy Experiments",
        "",
    ]
    if not summary.get("recent_market_strategies"):
        lines.append("No market-strategy experiments yet.")
    for item in summary.get("recent_market_strategies", [])[:20]:
        latest = item.get("evaluation", {})
        decision = latest.get("decision")
        metrics = ((latest.get("outcomes") or {}).get("metrics") or {})
        lines.append(
            f"- `{item['strategy_lab_id']}` type=`{item.get('experiment_type')}` "
            f"status=`{item['status']}` decision=`{decision}` "
            f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
            f"win=`{metrics.get('win_rate')}` hypothesis={item.get('hypothesis')}"
        )
    lines.extend(["", "## Contract Repair Queue", ""])
    if not summary.get("contract_repair_queue"):
        lines.append("No Strategy Lab contracts currently need runtime repair.")
    for item in summary.get("contract_repair_queue", [])[:20]:
        lines.append(
            f"- `{item['strategy_lab_id']}` compile=`{item.get('compile_status')}` "
            f"reason=`{item.get('reason')}` missing=`{item.get('unsupported_required_fields')}`"
        )
    lines.extend(["", "## Non-Market Experiments", ""])
    if not summary.get("recent_non_market_experiments"):
        lines.append("No recent risk/system/reporting experiments.")
    for item in summary.get("recent_non_market_experiments", [])[:20]:
        latest = item.get("evaluation", {})
        decision = latest.get("decision")
        metrics = ((latest.get("outcomes") or {}).get("metrics") or {})
        lines.append(
            f"- `{item['strategy_lab_id']}` type=`{item.get('experiment_type')}` "
            f"status=`{item['status']}` decision=`{decision}` "
            f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
            f"win=`{metrics.get('win_rate')}` hypothesis={item.get('hypothesis')}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
