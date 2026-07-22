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
SUPPORTED_LOGIC_TYPES = {"candidate_filter", "candidate_selector", "candidate_transform"}
KNOWN_TRADE_TYPES = {
    "frontier_crypto_venue_map",
    "perp_funding_basis",
    "global_market_discovery_proxy",
    "global_proxy_momentum",
    "prediction_market_probability",
}
KNOWN_DIRECTIONS = {
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
DIRECTION_TRADE_TYPE_HINTS = {
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


def _normalize_strategy_logic(logic: dict) -> dict:
    normalized = dict(logic)
    notes = _as_list(normalized.get("normalization_notes"))

    trade_types = _as_list(normalized.get("trade_types") or normalized.get("source_trade_types") or normalized.get("trade_type"))
    directions = _as_list(normalized.get("directions") or normalized.get("allowed_directions") or normalized.get("direction"))

    cleaned_trade_types: list[str] = []
    repaired_directions = list(directions)
    for item in trade_types:
        token = item.strip()
        lowered = token.lower()
        if lowered in KNOWN_DIRECTIONS:
            repaired_directions.append(lowered)
            notes.append(f"moved_trade_type_direction:{lowered}")
            continue
        if lowered in GENERIC_DIRECTIONS:
            repaired_directions.append(lowered)
            notes.append(f"moved_trade_type_generic_direction:{lowered}")
            continue
        cleaned_trade_types.append(lowered if lowered in KNOWN_TRADE_TYPES else token)

    normalized_directions: list[str] = []
    for item in repaired_directions:
        token = item.strip()
        lowered = token.lower()
        if lowered in KNOWN_DIRECTIONS or lowered in GENERIC_DIRECTIONS:
            normalized_directions.append(lowered)
        else:
            normalized_directions.append(token)

    inferred_trade_types = [
        DIRECTION_TRADE_TYPE_HINTS[direction]
        for direction in normalized_directions
        if direction in DIRECTION_TRADE_TYPE_HINTS
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
    if notes:
        normalized["normalization_notes"] = _unique(notes)
    return normalized


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
        status = "needs_data"
    else:
        status = "active_testing"
    logic["type"] = logic_type
    logic = _normalize_strategy_logic(logic)

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
        contract["status"],
        contract["hypothesis"],
        json.dumps(contract["strategy_logic"], sort_keys=True),
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
                strategy_lab_id, version, parent_strategy_lab_id, status, hypothesis,
                strategy_logic_json, data_requirements_json, risk_gates_json,
                promotion_rules_json, source_agent, source_recommendation_id,
                created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                strategy_logic_json = ?,
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
                json.dumps(contract["strategy_logic"], sort_keys=True),
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
            "status": contract["status"],
            "created": created,
        }
    ]


def _active_experiments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select *
        from strategy_lab_experiments
        where status in ('active_testing')
        order by updated_at desc
        limit 100
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["strategy_logic"] = _json_loads(item.pop("strategy_logic_json"), {})
        item["strategy_logic"] = _normalize_strategy_logic(item["strategy_logic"])
        item["data_requirements"] = _json_loads(item.pop("data_requirements_json"), {})
        item["risk_gates"] = _json_loads(item.pop("risk_gates_json"), {})
        item["promotion_rules"] = _json_loads(item.pop("promotion_rules_json"), {})
        item["evaluation"] = _json_loads(item.pop("evaluation_json"), {})
        output.append(item)
    return output


def _allowed(value: Any, allowed: list[str]) -> bool:
    return not allowed or str(value) in set(allowed)


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


def _matches_logic(candidate: dict, logic: dict, risk_gates: dict, settings: dict) -> tuple[bool, list[str]]:
    if candidate.get("strategy_lab_id"):
        return False, ["already_strategy_lab_candidate"]
    reasons = []
    allowed_trade_types = _as_list(logic.get("trade_types") or logic.get("source_trade_types"))
    allowed_venues = _as_list(logic.get("venues") or logic.get("allowed_venues"))
    allowed_directions = _as_list(logic.get("directions") or logic.get("allowed_directions"))
    allowed_regions = _as_list(logic.get("regions") or logic.get("allowed_regions"))
    allowed_asset_classes = _as_list(logic.get("asset_classes") or logic.get("allowed_asset_classes"))
    if not _allowed(candidate.get("trade_type"), allowed_trade_types):
        reasons.append("trade_type_not_allowed")
    if not _allowed(candidate.get("venue"), allowed_venues):
        reasons.append("venue_not_allowed")
    if not _direction_allowed(candidate.get("direction"), allowed_directions):
        reasons.append("direction_not_allowed")
    if not _allowed(candidate.get("region"), allowed_regions):
        reasons.append("region_not_allowed")
    if not _allowed(candidate.get("asset_class"), allowed_asset_classes):
        reasons.append("asset_class_not_allowed")

    risk = settings.get("risk", {})
    min_edge = _as_float(risk_gates.get("min_edge_bps", logic.get("min_edge_bps")), _as_float(risk.get("min_net_edge_bps"), 2.0))
    min_score = _as_float(risk_gates.get("min_score", logic.get("min_score")), 0.0)
    min_liquidity = _as_float(
        risk_gates.get("min_liquidity_score", logic.get("min_liquidity_score")),
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
    if _as_float(candidate.get("score")) < min_score:
        reasons.append("score_below_gate")
    if _as_float(candidate.get("liquidity_score")) < min_liquidity:
        reasons.append("liquidity_below_gate")
    if _as_float(candidate.get("spread_bps"), 999.0) > max_spread:
        reasons.append("spread_above_gate")
    if min_quality is not None and _as_float(candidate.get("quality_score"), 0.0) < _as_float(min_quality):
        reasons.append("quality_below_gate")
    if max_stale is not None and _as_float(candidate.get("stale_minutes"), 0.0) > _as_float(max_stale):
        reasons.append("stale_above_gate")

    required_fields = _as_list(logic.get("required_fields") or risk_gates.get("required_fields"))
    missing = [field for field in required_fields if candidate.get(field) is None]
    if missing:
        reasons.append("missing_required_fields:" + ",".join(missing[:5]))
    return not reasons, reasons


def generate_strategy_lab_candidates(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    price_observations: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("enabled", True):
        return [], {"enabled": False, "generated_candidates": 0}

    experiments = _active_experiments(conn)
    max_total = int(cfg.get("max_candidates_per_loop", 25))
    max_per_experiment = int(cfg.get("max_candidates_per_experiment", 5))
    default_bonus = float(cfg.get("candidate_score_bonus", 1.0))
    max_bonus = float(cfg.get("max_candidate_score_bonus", 5.0))
    generated = []
    per_experiment: dict[str, int] = Counter()
    rejects: dict[str, Counter] = defaultdict(Counter)

    pool = sorted(candidates, key=lambda row: _as_float(row.get("score")), reverse=True)
    for experiment in experiments:
        if len(generated) >= max_total:
            break
        logic = experiment.get("strategy_logic") or {}
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
                continue
            lab_candidate = dict(candidate)
            lab_candidate["strategy_lab_id"] = experiment["strategy_lab_id"]
            lab_candidate["strategy_lab_version"] = int(experiment["version"])
            lab_candidate["strategy_lab_hypothesis"] = experiment["hypothesis"]
            lab_candidate["strategy_lab_logic_type"] = logic.get("type", "candidate_filter")
            lab_candidate["strategy_lab_source_trade_type"] = candidate.get("trade_type")
            lab_candidate["strategy_lab_source_signal_key"] = candidate.get("signal_key")
            lab_candidate["strategy_lab_candidate"] = True
            lab_candidate["score"] = round(min(100.0, _as_float(candidate.get("score")) + bonus), 3)
            lab_candidate["edge_bps_estimate"] = round(max(0.0, _candidate_edge(candidate) + edge_bonus), 3)
            lab_candidate["thesis"] = (
                f"Strategy Lab {experiment['strategy_lab_id']}: {experiment['hypothesis']}"
            )[:1000]
            generated.append(lab_candidate)
            per_experiment[experiment["strategy_lab_id"]] += 1

    report = {
        "enabled": True,
        "generated_at": _utc(),
        "active_experiments": len(experiments),
        "source_candidate_count": len(candidates),
        "price_observation_count": len(price_observations or []),
        "generated_candidates": len(generated),
        "generated_by_experiment": dict(per_experiment),
        "reject_reasons_by_experiment": {key: dict(value) for key, value in rejects.items()},
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
            "evaluation": evaluation,
            "promotion_rules": rules,
        },
        "proposed_change": {
            "summary": "Turn the proven Strategy Lab contract into a stable paper-only scanner/strategy implementation.",
            "strategy_contract": {
                "strategy_lab_id": experiment["strategy_lab_id"],
                "version": experiment["version"],
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
                    strategy_lab_id, version, parent_strategy_lab_id, status, hypothesis,
                    strategy_logic_json, data_requirements_json, risk_gates_json,
                    promotion_rules_json, source_agent, source_recommendation_id,
                    created_at, updated_at
                ) values (?, ?, ?, 'active_testing', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    1,
                    experiment["strategy_lab_id"],
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
        decision = "needs_more_evidence"
        status = "active_testing"
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
                "status": status,
                "decision": decision,
                "metrics": metrics,
                "valid_label_rate": outcomes["valid_label_rate"],
                "promotion_recommendation_id": promotion_id,
            }
        )
    return {"enabled": True, "evaluated": evaluations}


def strategy_lab_summary(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        select strategy_lab_id, version, parent_strategy_lab_id, status, hypothesis,
               created_at, updated_at, last_evaluated_at, evaluation_json,
               consecutive_passes, promoted_proposal_id
        from strategy_lab_experiments
        order by updated_at desc
        limit ?
        """,
        (int(limit),),
    ).fetchall()
    items = []
    status_counts: Counter = Counter()
    for row in rows:
        item = dict(row)
        item["evaluation"] = _json_loads(item.pop("evaluation_json"), {})
        status_counts[item["status"]] += 1
        items.append(item)
    total = conn.execute("select count(*) as n from strategy_lab_experiments").fetchone()
    generated_candidates = 0
    if REPORT_JSON.exists():
        latest = _json_loads(REPORT_JSON.read_text(encoding="utf-8"), {})
        generated_candidates = int((latest.get("generation") or {}).get("generated_candidates") or 0)
    return {
        "enabled": True,
        "total_experiments": int(total["n"] if total else 0),
        "status_counts": dict(status_counts),
        "recent": items,
        "generated_candidates_last_cycle": generated_candidates,
        "report": str(REPORT_MD),
    }


def write_strategy_lab_reports(conn: sqlite3.Connection, generation: dict | None = None, evaluation: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    summary = strategy_lab_summary(conn)
    report = {
        "generated_at": _utc(),
        "summary": summary,
        "generation": generation or {},
        "evaluation": evaluation or {},
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
        f"- Candidates generated this loop: `{(generation or {}).get('generated_candidates', 0)}`",
        "",
        "## Recent Experiments",
        "",
    ]
    if not summary.get("recent"):
        lines.append("No Strategy Lab experiments yet.")
    for item in summary.get("recent", [])[:20]:
        latest = item.get("evaluation", {})
        decision = latest.get("decision")
        metrics = ((latest.get("outcomes") or {}).get("metrics") or {})
        lines.append(
            f"- `{item['strategy_lab_id']}` status=`{item['status']}` decision=`{decision}` "
            f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
            f"win=`{metrics.get('win_rate')}` hypothesis={item.get('hypothesis')}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
