"""Paper-only Strategy Lab experiments.

Candidate filters remain supported. Observation programs additionally let the
lab derive independent candidates from normalized market history without
executing arbitrary model code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from route_resolver import evaluate_route_intelligence
from paper_context_cost import realized_paper_cost_audit
from frontier_data_quality import paper_only_yahoo_proxy_cross_surface_alignment_guard
from signals.registry import discover_signals, known_strategy_signatures, promoted_strategy_lab_ids
from storage import (
    RUNS_DIR,
    add_llm_recommendation,
    link_recommendation_artifact,
    signal_key,
    utc_now,
)
from strategy_reliability import (
    hydrate_paper_lineage_source_health,
    paper_lineage_source_health_record,
    paper_source_veto_record,
    paper_source_veto_recovery_status,
)
from strategy_program import (
    LOGIC_TYPE as OBSERVATION_PROGRAM,
    PROGRAM_CANDIDATE_PASSTHROUGH_FIELDS,
    compile_observation_program,
    generate_program_candidates,
    novelty_signature,
    record_feature_snapshots,
)
from strategy_feasibility import (
    maybe_create_relaxed_child,
    profile_observation_program,
    record_contract_evaluation,
)


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
    "quarantined_surface_policy",
}
EXPERIMENT_TYPES = {
    "market_strategy",
    "risk_filter",
    "execution_filter",
    "system_repair",
    "reporting_quality",
}
DEFAULT_EXPERIMENT_TYPE = "market_strategy"
SUPPORTED_LOGIC_TYPES = {
    "candidate_filter",
    "candidate_selector",
    "candidate_transform",
    OBSERVATION_PROGRAM,
}
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

SURFACE_TARGET_FIELDS = (
    "target_surface",
    "market_surface",
    "execution_surface",
    "route_surface",
    "asset_surface",
)
YAHOO_PROXY_SURFACE = "yahoo_proxy"
PROGRAM_OBSERVATION_ENRICHMENT_FIELDS = PROGRAM_CANDIDATE_PASSTHROUGH_FIELDS | {
    "spread_bps",
    "liquidity_score",
    "quality_score",
    "quality_status",
    "funding_bps",
    "funding_history_count",
    "funding_history_avg_bps",
    "funding_history_last_bps",
    "funding_interval_hours",
    "next_funding_time",
    "time_to_next_funding_minutes",
    "basis_bps",
    "net_carry_edge_bps",
    "round_trip_cost_bps",
    "estimated_round_trip_cost_bps",
    "quote_volume_24h",
    "change_24h_pct",
    "freshness_age_seconds",
    "stale_minutes",
    "data_status",
    "asset_class",
    "market_type",
    "region",
    "base",
    "quote",
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


def _normalize_surface(value: Any) -> str | None:
    """Normalize an explicit surface label without equating distinct markets."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = []
    for char in text:
        if char.isalnum():
            normalized.append(char)
        elif normalized and normalized[-1] != "_":
            normalized.append("_")
    return "".join(normalized).strip("_") or None


def _surface_values(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    return _unique(
        [surface for item in raw if (surface := _normalize_surface(item)) is not None]
    )


def _requested_target_surface(sources: list[dict]) -> str | None:
    for source in sources:
        containers = [source]
        logic = source.get("strategy_logic")
        if isinstance(logic, dict):
            containers.append(logic)
        for container in containers:
            for field in (
                "requested_target_surface",
                "target_surface",
                "destination_surface",
                "route_surface",
                "execution_surface",
            ):
                target = _normalize_surface(container.get(field))
                if target:
                    return target
    return None


def _yahoo_proxy_surface_review(
    source_surface: str | None,
    permitted: list[str],
    requested_target_surface: str | None = None,
) -> dict:
    """Require explicit same-surface validation for Yahoo-derived artifacts."""
    applies = source_surface == YAHOO_PROXY_SURFACE
    blocked_targets = [surface for surface in permitted if surface != source_surface]
    source_explicitly_permitted = bool(source_surface and source_surface in permitted)
    requested_target_eligible = requested_target_surface is None or (
        requested_target_surface == source_surface
        and requested_target_surface in permitted
    )
    eligible = not applies or (
        source_explicitly_permitted
        and not blocked_targets
        and requested_target_eligible
    )
    return {
        "applies": applies,
        "eligible": eligible,
        "reason": (
            "yahoo_proxy_same_surface_validated"
            if applies and eligible
            else "yahoo_proxy_same_surface_required"
            if applies
            else "not_applicable"
        ),
        "source_explicitly_permitted": source_explicitly_permitted,
        "requested_target_surface": requested_target_surface,
        "blocked_target_surfaces": blocked_targets,
        "paper_only": True,
    }


def _surface_contract(payload: dict, contract: dict, data_requirements: dict) -> dict:
    sources = [contract, data_requirements, payload]
    source_surface = None
    permitted: list[str] = []
    for source in sources:
        if source_surface is None:
            source_surface = _normalize_surface(source.get("source_surface"))
        if not permitted:
            permitted = _surface_values(
                source.get("permitted_target_surface")
                or source.get("permitted_target_surfaces")
            )
    missing = []
    if source_surface is None:
        missing.append("source_surface")
    if not permitted:
        missing.append("permitted_target_surface")
    requested_target_surface = _requested_target_surface(sources)
    yahoo_review = _yahoo_proxy_surface_review(
        source_surface,
        permitted,
        requested_target_surface,
    )
    eligible = not missing and yahoo_review["eligible"]
    reason = (
        "missing_surface_metadata"
        if missing
        else yahoo_review["reason"]
        if yahoo_review["applies"]
        else "surface_contract_valid"
    )
    return {
        "source_surface": source_surface,
        "permitted_target_surface": permitted,
        "requested_target_surface": requested_target_surface,
        "eligible": eligible,
        "reason": reason,
        "missing_fields": missing,
        "review_required": not eligible,
        "yahoo_proxy_same_surface_review": yahoo_review,
        "paper_only": True,
    }


def _candidate_target_surface(candidate: dict) -> str | None:
    for key in SURFACE_TARGET_FIELDS:
        surface = _normalize_surface(candidate.get(key))
        if surface:
            return surface
    target_segment = candidate.get("target_segment")
    if isinstance(target_segment, dict):
        return _normalize_surface(
            target_segment.get("target_surface")
            or target_segment.get("surface")
            or target_segment.get("market_surface")
            or target_segment.get("execution_surface")
        )
    return None


def _surface_compatibility(experiment: dict, candidate: dict) -> dict:
    source_surface = _normalize_surface(experiment.get("source_surface"))
    permitted = _surface_values(experiment.get("permitted_target_surface"))
    if not permitted:
        permitted = _surface_values(experiment.get("permitted_target_surfaces"))
    target_surface = _candidate_target_surface(candidate)
    missing = []
    if source_surface is None:
        missing.append("source_surface")
    if not permitted:
        missing.append("permitted_target_surface")
    if target_surface is None:
        missing.append("target_surface")
    yahoo_review = _yahoo_proxy_surface_review(
        source_surface,
        permitted,
        target_surface,
    )
    eligible = not missing and target_surface in permitted and yahoo_review["eligible"]
    reason = (
        "surface_compatible"
        if eligible
        else "missing_surface_metadata"
        if missing
        else yahoo_review["reason"]
        if yahoo_review["applies"] and not yahoo_review["eligible"]
        else "target_surface_not_permitted"
    )
    return {
        "eligible": eligible,
        "reason": reason,
        "source_surface": source_surface,
        "target_surface": target_surface,
        "permitted_target_surface": permitted,
        "missing_fields": missing,
        "review_required": not eligible,
        "yahoo_proxy_same_surface_review": yahoo_review,
        "paper_only": True,
    }


def strategy_lab_surface_activation_eligible(candidate: dict) -> bool:
    """Recheck Yahoo same-surface lineage instead of trusting a cached verdict."""
    policy = candidate.get("strategy_lab_surface_policy")
    if not isinstance(policy, dict) or policy.get("eligible") is not True:
        return False
    candidate_source = _normalize_surface(candidate.get("source_surface"))
    policy_source = _normalize_surface(policy.get("source_surface"))
    if candidate_source and policy_source and candidate_source != policy_source:
        return False
    source_surface = policy_source or candidate_source
    if source_surface != YAHOO_PROXY_SURFACE:
        return True
    candidate_permitted = _surface_values(candidate.get("permitted_target_surface"))
    policy_permitted = _surface_values(policy.get("permitted_target_surface"))
    if candidate_permitted and policy_permitted and candidate_permitted != policy_permitted:
        return False
    candidate_target = _candidate_target_surface(candidate)
    policy_target = _normalize_surface(policy.get("target_surface"))
    if candidate_target and policy_target and candidate_target != policy_target:
        return False
    experiment = {
        "source_surface": source_surface,
        "permitted_target_surface": policy_permitted or candidate_permitted,
    }
    activation_candidate = dict(candidate)
    if candidate_target is None:
        activation_candidate["target_surface"] = policy_target
    return _surface_compatibility(experiment, activation_candidate)["eligible"]


def enforce_promoted_strategy_lab_surface_policy(
    conn: sqlite3.Connection,
    candidates: list[dict],
) -> tuple[list[dict], dict]:
    """Block promoted Yahoo-derived plugins that escape their source surface.

    Promoted signal plugins run before the ordinary Strategy Lab candidate
    selector.  Rebuild their activation verdict from the persisted experiment
    so generated code cannot broaden its own permitted target surfaces.
    """
    strategy_ids = _unique(
        [
            str(candidate.get("strategy_lab_id") or "").strip()
            for candidate in candidates
            if str(candidate.get("strategy_lab_id") or "").strip()
        ]
    )
    if not strategy_ids:
        return list(candidates), {
            "checked_candidate_count": 0,
            "blocked_candidate_count": 0,
            "quarantined_artifact_count": 0,
            "quarantined_strategy_lab_ids": [],
            "paper_only": True,
        }

    placeholders = ",".join("?" for _ in strategy_ids)
    rows = conn.execute(
        f"""
        select strategy_lab_id, status, source_surface,
               permitted_target_surfaces_json, surface_policy_json
        from strategy_lab_experiments
        where strategy_lab_id in ({placeholders})
        """,
        strategy_ids,
    ).fetchall()
    experiments = {str(row["strategy_lab_id"]): dict(row) for row in rows}
    reviews: dict[str, list[dict]] = defaultdict(list)

    for candidate in candidates:
        strategy_lab_id = str(candidate.get("strategy_lab_id") or "").strip()
        if not strategy_lab_id:
            continue
        row = experiments.get(strategy_lab_id)
        stored_policy = _json_loads(row.get("surface_policy_json"), {}) if row else {}
        if not isinstance(stored_policy, dict):
            stored_policy = {}
        row_source = _normalize_surface(row.get("source_surface")) if row else None
        candidate_source = _normalize_surface(candidate.get("source_surface"))
        policy_source = _normalize_surface(stored_policy.get("source_surface"))
        declared_sources = {value for value in (row_source, candidate_source, policy_source) if value}
        if YAHOO_PROXY_SURFACE not in declared_sources:
            continue

        source_conflict = len(declared_sources) > 1
        if row is not None:
            permitted = _surface_values(
                _json_loads(row.get("permitted_target_surfaces_json"), [])
            )
        else:
            permitted = _surface_values(
                stored_policy.get("permitted_target_surface")
                or candidate.get("permitted_target_surface")
            )
        candidate_target = _candidate_target_surface(candidate)
        policy_target = _normalize_surface(stored_policy.get("target_surface"))
        target_conflict = bool(
            candidate_target and policy_target and candidate_target != policy_target
        )
        activation_candidate = dict(candidate)
        if candidate_target is None:
            activation_candidate["target_surface"] = policy_target
        compatibility = _surface_compatibility(
            {
                "source_surface": YAHOO_PROXY_SURFACE,
                "permitted_target_surface": permitted,
            },
            activation_candidate,
        )
        requested_target = _normalize_surface(stored_policy.get("requested_target_surface"))
        contract_review = _yahoo_proxy_surface_review(
            YAHOO_PROXY_SURFACE,
            permitted,
            requested_target,
        )
        status_quarantined = bool(
            row and row.get("status") == "quarantined_surface_policy"
        )
        eligible = bool(
            compatibility["eligible"]
            and contract_review["eligible"]
            and not source_conflict
            and not target_conflict
            and not status_quarantined
        )
        reason = (
            "surface_metadata_conflict"
            if source_conflict or target_conflict
            else "artifact_already_surface_quarantined"
            if status_quarantined
            else contract_review["reason"]
            if not contract_review["eligible"]
            else compatibility["reason"]
        )
        reviews[strategy_lab_id].append(
            {
                **compatibility,
                "eligible": eligible,
                "reason": reason,
                "requested_target_surface": requested_target,
                "source_metadata_conflict": source_conflict,
                "target_metadata_conflict": target_conflict,
                "activation_blocked": not eligible,
            }
        )

    blocked_ids = {
        strategy_lab_id
        for strategy_lab_id, artifact_reviews in reviews.items()
        if any(not review["eligible"] for review in artifact_reviews)
    }
    now = _utc()
    for strategy_lab_id in sorted(blocked_ids):
        row = experiments.get(strategy_lab_id)
        if row is None:
            continue
        artifact_reviews = reviews[strategy_lab_id]
        persisted_review = next(
            review for review in artifact_reviews if not review["eligible"]
        )
        persisted_review = {
            **persisted_review,
            "artifact_candidate_count": len(artifact_reviews),
            "quarantined_at_activation": True,
        }
        conn.execute(
            """
            update strategy_lab_experiments
            set status = 'quarantined_surface_policy',
                compile_status = 'surface_quarantined',
                surface_policy_json = ?, updated_at = ?
            where strategy_lab_id = ?
            """,
            (json.dumps(persisted_review, sort_keys=True), now, strategy_lab_id),
        )
    if blocked_ids:
        conn.commit()

    filtered = [
        candidate
        for candidate in candidates
        if str(candidate.get("strategy_lab_id") or "").strip() not in blocked_ids
    ]
    checked_count = sum(len(items) for items in reviews.values())
    blocked_count = len(candidates) - len(filtered)
    return filtered, {
        "checked_candidate_count": checked_count,
        "blocked_candidate_count": blocked_count,
        "quarantined_artifact_count": len(blocked_ids),
        "quarantined_strategy_lab_ids": sorted(blocked_ids),
        "paper_only": True,
    }


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
    if str(normalized.get("type") or normalized.get("logic_type")) == OBSERVATION_PROGRAM:
        normalized["type"] = OBSERVATION_PROGRAM
        return normalized
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
    if str(logic.get("type") or logic.get("logic_type")) == OBSERVATION_PROGRAM:
        return True
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
    if str(logic.get("type") or logic.get("logic_type")) == OBSERVATION_PROGRAM:
        return "market_strategy"

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


def _explicit_values(*values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        if isinstance(value, str):
            pieces = value.replace(";", ",").split(",")
            output.extend(piece.strip() for piece in pieces if piece.strip())
        elif isinstance(value, (list, tuple, set)):
            output.extend(str(item).strip() for item in value if str(item).strip())
    return _unique(output)


def _first_explicit(sources: list[dict], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _structured_trade_type(sources: list[dict]) -> str | None:
    raw = str(
        _first_explicit(
            sources,
            "trade_type",
            "market_key",
            "strategy_family",
            "asset_surface",
            "universe",
        )
        or ""
    ).strip().lower()
    if raw in FALLBACK_TRADE_TYPE_EXAMPLES:
        return raw
    if raw in {"frontier_spot", "frontier_crypto", "frontier_crypto_spot"}:
        return "frontier_crypto_venue_map"
    if raw in {"okx_perp_funding_basis", "funding_capture", "perp_basis"}:
        return "perp_funding_basis"
    if raw in {"global_proxy", "country_proxy", "equity_proxy"}:
        return "global_market_discovery_proxy"
    if raw in {"yahoo_proxy", "proxy_momentum"}:
        return "global_proxy_momentum"
    if raw in {"prediction_market", "event_market"}:
        return "prediction_market_probability"
    return None


def _structured_directions(sources: list[dict], trade_type: str) -> list[str]:
    raw_values = _explicit_values(
        *[
            source.get(key)
            for source in sources
            for key in ("directions", "allowed_directions", "direction", "mode", "direction_mode")
        ]
    )
    directions: list[str] = []
    for raw in raw_values:
        token = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if token in FALLBACK_DIRECTION_EXAMPLES:
            directions.append(token)
            continue
        is_long = token in {"long", "long_only"} or "long_only" in token
        is_short = token in {"short", "short_only"} or "short_only" in token
        if trade_type == "frontier_crypto_venue_map":
            if is_long:
                directions.append("long_frontier_spot")
            if is_short:
                directions.append("short_frontier_spot")
        elif trade_type in {"global_market_discovery_proxy", "global_proxy_momentum"}:
            if is_long:
                directions.append("long_proxy")
            if is_short:
                directions.append("short_proxy")
        elif trade_type == "perp_funding_basis" and "funding" in token:
            if is_long:
                directions.append("funding_capture_long_perp")
            if is_short:
                directions.append("funding_capture_short_perp")
    return _unique(directions)


def _structured_venues(sources: list[dict]) -> list[str]:
    venues: list[str] = []
    for source in sources:
        venues.extend(_explicit_values(source.get("venues"), source.get("allowed_venues"), source.get("venue")))
        venues.extend(
            str(value).strip()
            for key, value in source.items()
            if key.startswith("include_venue") and value not in (None, "")
        )
    return _unique([venue.upper() for venue in venues if venue])


def _structured_strategy_contract(payload: dict, proposed: dict) -> dict | None:
    """Compile explicit model fields into the existing bounded lab contract.

    This intentionally does not infer market scope from prose. It only accepts
    structured market, direction, venue, and threshold fields supplied by the
    agent, so new surfaces remain flexible without guessing implementation.
    """

    variant = payload.get("variant_config") if isinstance(payload.get("variant_config"), dict) else {}
    nested_filters = variant.get("filters") if isinstance(variant.get("filters"), dict) else {}
    sources = [nested_filters, variant, proposed, payload]
    trade_type = _structured_trade_type(sources)
    if not trade_type:
        return None
    directions = _structured_directions(sources, trade_type)
    if not directions:
        return None
    venues = _structured_venues(sources)

    logic: dict[str, Any] = {
        "type": "candidate_filter",
        "trade_types": [trade_type],
        "directions": directions,
    }
    if venues:
        logic["venues"] = venues

    required_fields: list[str] = []
    risk_gates: dict[str, Any] = {}
    max_spread = _first_explicit(sources, "max_spread_bps", "spread_bps_max", "max_entry_spread_bps")
    min_liquidity = _first_explicit(sources, "min_liquidity_score", "liquidity_score_floor")
    min_score = _first_explicit(
        sources,
        "min_composite_score",
        "min_score",
        "min_confidence_score",
        "min_confidence",
    )
    min_quality = _first_explicit(sources, "min_quality_score", "quality_score_floor")
    min_edge = _first_explicit(sources, "min_edge_bps", "min_depth_adjusted_edge_bps")
    max_stale_minutes = _first_explicit(sources, "max_stale_minutes")
    max_age_seconds = _first_explicit(sources, "max_signal_age_seconds", "freshness_horizon_seconds")
    max_age_hours = _first_explicit(sources, "listing_freshness_max_hours", "max_age_hours")
    for key, value, field in (
        ("max_spread_bps", max_spread, "spread_bps"),
        ("min_liquidity_score", min_liquidity, "liquidity_score"),
        ("min_score", min_score, "score"),
        ("min_quality_score", min_quality, "quality_score"),
        ("min_edge_bps", min_edge, "edge_bps_estimate"),
    ):
        if value is None:
            continue
        numeric = _as_float(value, math.nan)
        if math.isfinite(numeric):
            logic[key] = numeric
            risk_gates[key] = numeric
            required_fields.append(field)
    if max_stale_minutes is None and max_age_seconds is not None:
        max_stale_minutes = _as_float(max_age_seconds) / 60.0
    if max_stale_minutes is None and max_age_hours is not None:
        max_stale_minutes = _as_float(max_age_hours) * 60.0
    if max_stale_minutes is not None:
        stale = _as_float(max_stale_minutes, math.nan)
        if math.isfinite(stale) and stale >= 0:
            logic["max_stale_minutes"] = stale
            risk_gates["max_stale_minutes"] = stale
            required_fields.append("seen_at")
    if required_fields:
        logic["required_fields"] = _unique(required_fields)

    variant_name = str(variant.get("variant_name") or proposed.get("variant_name") or "").strip()
    hypothesis = str(payload.get("rationale") or proposed.get("hypothesis") or payload.get("title") or "").strip()
    digest = hashlib.sha256(
        json.dumps(
            {
                "trade_type": trade_type,
                "directions": directions,
                "venues": venues,
                "logic": logic,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]
    strategy_lab_id = f"{_slug(variant_name or payload.get('title') or hypothesis)}_{digest}"
    source_surface = _first_explicit(sources, "source_surface")
    permitted_target_surface = _first_explicit(
        sources,
        "permitted_target_surface",
        "permitted_target_surfaces",
    )
    return {
        "strategy_lab_id": strategy_lab_id,
        "version": 1,
        "experiment_type": "market_strategy",
        "hypothesis": hypothesis,
        "source_surface": source_surface,
        "permitted_target_surface": permitted_target_surface,
        "strategy_logic": logic,
        "data_requirements": {
            "paper_only": True,
            "structured_contract_bridge": True,
            "source_market_key": payload.get("market_key"),
        },
        "risk_gates": risk_gates,
        "promotion_rules": {},
    }


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
    if not _first_dict(
        contract.get("strategy_logic"),
        contract.get("strategy_logic_json"),
        contract.get("logic"),
    ):
        structured = _structured_strategy_contract(
            payload,
            proposed if isinstance(proposed, dict) else contract,
        )
        if structured:
            contract = structured
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
    if logic_type == OBSERVATION_PROGRAM:
        compiled_program, program_diagnostic = compile_observation_program(logic)
        if compiled_program:
            logic = compiled_program
        elif program_diagnostic.get("status") == "needs_feature_code":
            status = "needs_data"
        else:
            status = "rejected_invalid"
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
    for target, source in (
        ("source_market_key", "market_key"),
        ("source_signal_key", "signal_key"),
        ("source_strategy_lab_parent", "strategy_lab_parent"),
        ("source_strategy_lab_variant", "strategy_lab_variant"),
    ):
        if payload.get(source) not in (None, ""):
            data_requirements.setdefault(target, payload[source])
    if experiment_type == "market_strategy":
        surface_policy = _surface_contract(payload, contract, data_requirements)
        data_requirements["source_surface"] = surface_policy["source_surface"]
        data_requirements["permitted_target_surface"] = surface_policy["permitted_target_surface"]
        data_requirements["surface_policy_required"] = True
        if not surface_policy["eligible"]:
            status = "quarantined_surface_policy"
    else:
        surface_policy = {
            "source_surface": None,
            "permitted_target_surface": [],
            "eligible": True,
            "reason": "non_market_experiment_not_applicable",
            "missing_fields": [],
            "review_required": False,
            "paper_only": True,
        }
    parent = contract.get("parent_strategy_lab_id")

    return {
        "strategy_lab_id": strategy_lab_id,
        "version": version,
        "parent_strategy_lab_id": str(parent).strip() if parent else None,
        "experiment_type": experiment_type,
        "status": status,
        "hypothesis": hypothesis,
        "source_surface": surface_policy["source_surface"],
        "permitted_target_surface": surface_policy["permitted_target_surface"],
        "surface_policy": surface_policy,
        "strategy_logic": logic,
        "data_requirements": data_requirements,
        "risk_gates": risk_gates,
        "promotion_rules": promotion_rules,
    }, None


def ingest_strategy_lab_recommendation(
    conn: sqlite3.Connection,
    rec: dict,
    settings: dict | None = None,
) -> list[dict]:
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

    veto_subject = dict(payload)
    veto_subject["validated_strategy_contract"] = contract
    source_veto = paper_source_veto_record(veto_subject, settings)
    if source_veto is not None and contract["status"] != "quarantined_surface_policy":
        return [
            {
                "action_status": "skipped",
                "artifact": "strategy_lab_experiment",
                "reason": "paper_source_family_veto",
                "strategy_lab_id": contract["strategy_lab_id"],
                "paper_only": True,
                "source_veto": source_veto,
            }
        ]

    now = _utc()
    logic_type = str(contract["strategy_logic"].get("type") or "candidate_filter")
    signature = novelty_signature(contract["strategy_logic"]) if logic_type == OBSERVATION_PROGRAM else None
    duplicate = None
    promoted_match = None
    if signature:
        duplicate = conn.execute(
            """
            select strategy_lab_id
            from strategy_lab_experiments
            where novelty_signature = ? and strategy_lab_id <> ?
            order by created_at
            limit 1
            """,
            (signature, contract["strategy_lab_id"]),
        ).fetchone()
        discover_signals()
        promoted_match = known_strategy_signatures().get(signature)
        if (duplicate or promoted_match) and contract["status"] != "quarantined_surface_policy":
            contract["status"] = "rejected_invalid"
    novelty_status = (
        "duplicate_experiment"
        if duplicate
        else "matches_promoted_signal"
        if promoted_match
        else "novel"
        if signature
        else "not_applicable"
    )
    novelty_details = {
        "signature": signature,
        "duplicate_strategy_lab_id": duplicate["strategy_lab_id"] if duplicate else None,
        "promoted_signal_id": promoted_match,
    }
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
        contract["source_surface"],
        json.dumps(contract["permitted_target_surface"], sort_keys=True),
        json.dumps(contract["surface_policy"], sort_keys=True),
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
                promotion_rules_json, source_surface, permitted_target_surfaces_json, surface_policy_json,
                source_agent, source_recommendation_id,
                created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    when ? = 'quarantined_surface_policy' then ?
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
                source_surface = ?,
                permitted_target_surfaces_json = ?,
                surface_policy_json = ?,
                source_recommendation_id = coalesce(source_recommendation_id, ?)
            where strategy_lab_id = ?
            """,
            (
                now,
                contract["status"],
                contract["status"],
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
                contract["source_surface"],
                json.dumps(contract["permitted_target_surface"], sort_keys=True),
                json.dumps(contract["surface_policy"], sort_keys=True),
                rec.get("recommendation_id"),
                contract["strategy_lab_id"],
            ),
        )
        conn.commit()
        created = False

    conn.execute(
        """
        update strategy_lab_experiments
        set novelty_signature = ?, novelty_status = ?, novelty_details_json = ?
        where strategy_lab_id = ?
        """,
        (
            signature,
            novelty_status,
            json.dumps(novelty_details, sort_keys=True),
            contract["strategy_lab_id"],
        ),
    )
    link_recommendation_artifact(
        conn,
        rec.get("recommendation_id"),
        "strategy_lab_experiment",
        contract["strategy_lab_id"],
        "materialized_as" if created else "linked_existing",
        {"status": contract["status"], "experiment_type": contract["experiment_type"]},
    )
    conn.commit()

    return [
        {
            "action_status": (
                "quarantined"
                if contract["status"] == "quarantined_surface_policy"
                else "created"
            ),
            "artifact": "strategy_lab_experiment",
            "strategy_lab_id": contract["strategy_lab_id"],
            "experiment_type": contract["experiment_type"],
            "status": contract["status"],
            "surface_policy": contract["surface_policy"],
            "reason": (
                contract["surface_policy"]["reason"]
                if contract["status"] == "quarantined_surface_policy"
                else None
            ),
            "novelty_status": novelty_status,
            "source_veto": source_veto,
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
    quarantined = False
    for row in rows:
        item = dict(row)
        compiled_logic = item.pop("compiled_strategy_logic_json", None)
        fallback_logic = item.pop("strategy_logic_json", None)
        stored_logic = compiled_logic or fallback_logic
        item["strategy_logic"] = _json_loads(stored_logic, {})
        item.pop("original_strategy_logic_json", None)
        item["compile_diagnostics"] = _json_loads(item.pop("compile_diagnostics_json", None), {})
        item["novelty_details"] = _json_loads(item.pop("novelty_details_json", None), {})
        if str(item["strategy_logic"].get("type") or "") != OBSERVATION_PROGRAM:
            item["strategy_logic"] = _normalize_strategy_logic(item["strategy_logic"])
        item["experiment_type"] = _resolved_experiment_type(
            item.get("experiment_type"),
            item,
            item["strategy_logic"],
        )
        item["data_requirements"] = _json_loads(item.pop("data_requirements_json"), {})
        item["permitted_target_surface"] = _json_loads(
            item.pop("permitted_target_surfaces_json", None), []
        )
        item["surface_policy"] = _json_loads(item.pop("surface_policy_json", None), {})
        surface_contract = _surface_contract({}, item, item["data_requirements"])
        yahoo_review = surface_contract["yahoo_proxy_same_surface_review"]
        if yahoo_review["applies"] and not surface_contract["eligible"]:
            conn.execute(
                """
                update strategy_lab_experiments
                set status = 'quarantined_surface_policy',
                    compile_status = 'surface_quarantined',
                    surface_policy_json = ?, updated_at = ?
                where strategy_lab_id = ?
                """,
                (
                    json.dumps(surface_contract, sort_keys=True),
                    _utc(),
                    item["strategy_lab_id"],
                ),
            )
            quarantined = True
            continue
        item["risk_gates"] = _json_loads(item.pop("risk_gates_json"), {})
        item["promotion_rules"] = _json_loads(item.pop("promotion_rules_json"), {})
        item["evaluation"] = _json_loads(item.pop("evaluation_json"), {})
        output.append(item)
    if quarantined:
        conn.commit()
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
    if candidate.get("proxy_valid_for_reuse") is False:
        reasons.append("proxy_invalid_for_reuse")
    context_cost_gate = candidate.get("paper_context_cost_gate") or {}
    if (
        context_cost_gate.get("applicable")
        and context_cost_gate.get("enabled")
        and not context_cost_gate.get("eligible")
    ):
        reasons.append("paper_context_cost_floor_not_cleared")
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


def _queue_missing_feature_proposal(
    conn: sqlite3.Connection,
    experiment: dict,
    missing_features: list[str],
) -> str | None:
    signature = hashlib.sha256(
        json.dumps(
            {"strategy_lab_id": experiment["strategy_lab_id"], "missing_features": sorted(missing_features)},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    rec_id = f"strategy_lab_feature_extension_{signature}"
    payload = {
        "action": "propose_code_change",
        "priority": 88,
        "title": f"Add Strategy Lab features: {', '.join(sorted(missing_features)[:5])}",
        "rationale": "An observation-native strategy is valid in intent but requires feature calculations the runtime does not yet expose.",
        "market_key": f"strategy_lab.{experiment['strategy_lab_id']}",
        "signal_key": f"STRATEGY_LAB|{experiment['strategy_lab_id']}",
        "change_category": "runtime_pipeline_integration",
        "implementation_mode": "runtime_active",
        "evidence": {
            "strategy_lab_id": experiment["strategy_lab_id"],
            "missing_features": sorted(missing_features),
            "strategy_logic": experiment.get("strategy_logic") or {},
        },
        "proposed_change": {
            "summary": "Add the missing normalized market-history features, snapshot provenance, and deterministic tests without changing the strategy contract.",
            "missing_features": sorted(missing_features),
        },
        "code_change": {
            "change_category": "runtime_pipeline_integration",
            "implementation_mode": "runtime_active",
            "expected_files": ["src/strategy_program.py", "tests/test_strategy_program.py"],
            "tests_to_run": ["python -m unittest tests.test_strategy_program tests.test_strategy_lab"],
            "runtime_integration": {
                "entrypoint_file": "src/strategy_program.py",
                "entrypoint_symbol": "build_feature_frames",
                "invocation_path": "radar_loop.run_once -> strategy_lab.generate_strategy_lab_candidates -> strategy_program",
                "test_file": "tests/test_strategy_program.py",
                "behavioral_test": "The previously missing feature is calculated from stored observations and the unchanged program emits candidates.",
            },
            "rollback_criteria": "Revert if feature snapshots or the normal paper radar loop fail regression tests.",
        },
    }
    created = add_llm_recommendation(
        conn,
        rec_id,
        "propose_code_change",
        payload["title"],
        payload["rationale"],
        payload,
    )
    return rec_id if created else None


def _compile_strategy_lab_contracts(
    conn: sqlite3.Connection,
    candidates: list[dict],
    observation_frames: list[dict] | None = None,
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
        surface_experiment = {
            "source_surface": row.get("source_surface"),
            "permitted_target_surface": _json_loads(
                row.get("permitted_target_surfaces_json"), []
            ),
        }
        surface_contract = _surface_contract({}, surface_experiment, data_requirements)
        if not surface_contract["eligible"]:
            diagnostic = {
                "compiled_at": now,
                "compile_status": "surface_quarantined",
                "reason": surface_contract["reason"],
                "missing_fields": surface_contract["missing_fields"],
                "surface_policy": surface_contract,
                "review_required": True,
            }
            prior_evaluation = _json_loads(row.get("evaluation_json"), {})
            conn.execute(
                """
                update strategy_lab_experiments
                set compile_status = 'surface_quarantined', compile_diagnostics_json = ?,
                    status = 'quarantined_surface_policy', updated_at = ?,
                    last_compiled_at = ?, compile_attempts = compile_attempts + 1,
                    evaluation_json = ?, surface_policy_json = ?
                where strategy_lab_id = ?
                """,
                (
                    json.dumps(diagnostic, sort_keys=True),
                    now,
                    now,
                    json.dumps({**prior_evaluation, "contract_compilation": diagnostic}, sort_keys=True),
                    json.dumps(surface_contract, sort_keys=True),
                    row["strategy_lab_id"],
                ),
            )
            summary["surface_quarantined"] += 1
            diagnostics[str(row["strategy_lab_id"])] = diagnostic
            continue
        if str(original.get("type") or original.get("logic_type") or "") == OBSERVATION_PROGRAM:
            compiled_program, program_diagnostic = compile_observation_program(original)
            signature = novelty_signature(compiled_program or original) if original else None
            duplicate = conn.execute(
                """
                select strategy_lab_id
                from strategy_lab_experiments
                where novelty_signature = ? and strategy_lab_id <> ?
                  and status not in ('rejected_invalid', 'retired_bad_evidence', 'retired_no_activity')
                order by created_at
                limit 1
                """,
                (signature, row["strategy_lab_id"]),
            ).fetchone() if signature else None
            discover_signals()
            promoted_match = known_strategy_signatures().get(str(signature)) if signature else None
            missing_features = list(program_diagnostic.get("missing_features") or [])
            feature_proposal_id = None
            if program_diagnostic.get("status") == "compiled" and not duplicate and not promoted_match:
                compile_status = "compiled"
                status = "needs_more_evidence" if row.get("status") == "needs_more_evidence" else "active_testing"
                reason = "observation_program_compiled"
                novelty_status = "novel"
            elif duplicate or promoted_match:
                compile_status = "duplicate_program"
                status = "rejected_invalid"
                reason = "duplicate_observation_program"
                novelty_status = "duplicate_experiment" if duplicate else "matches_promoted_signal"
            elif missing_features:
                compile_status = "needs_data"
                status = "needs_data"
                reason = "missing_program_features"
                novelty_status = "unassessed"
                feature_proposal_id = _queue_missing_feature_proposal(
                    conn,
                    {**row, "strategy_logic": original},
                    missing_features,
                )
            else:
                compile_status = "invalid"
                status = "rejected_invalid"
                reason = str(program_diagnostic.get("reason") or "invalid_observation_program")
                novelty_status = "unassessed"
            diagnostic = {
                "compiled_at": now,
                "compile_status": compile_status,
                "reason": reason,
                "logic_type": OBSERVATION_PROGRAM,
                "source_observation_count": len(observation_frames or []),
                "missing_features": missing_features,
                "feature_code_recommendation_id": feature_proposal_id,
                "novelty_signature": signature,
                "duplicate_strategy_lab_id": duplicate["strategy_lab_id"] if duplicate else None,
                "promoted_signal_id": promoted_match,
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
                    status = ?, updated_at = ?, evaluation_json = ?, novelty_signature = ?,
                    novelty_status = ?, novelty_details_json = ?
                where strategy_lab_id = ?
                """,
                (
                    json.dumps(original, sort_keys=True),
                    json.dumps(compiled_program or original, sort_keys=True),
                    json.dumps(compiled_program, sort_keys=True) if compile_status == "compiled" else "{}",
                    compile_status,
                    json.dumps(diagnostic, sort_keys=True),
                    str(signature or "")[:16] or None,
                    now,
                    status,
                    now,
                    json.dumps({**prior_evaluation, "contract_compilation": diagnostic}, sort_keys=True),
                    signature,
                    novelty_status,
                    json.dumps(
                        {
                            "signature": signature,
                            "duplicate_strategy_lab_id": duplicate["strategy_lab_id"] if duplicate else None,
                            "promoted_signal_id": promoted_match,
                        },
                        sort_keys=True,
                    ),
                    row["strategy_lab_id"],
                ),
            )
            summary[compile_status] += 1
            diagnostics[str(row["strategy_lab_id"])] = diagnostic
            continue
        compatible_runtime_evidence: list[dict] = []
        surface_quarantined_evidence: list[dict] = []
        for candidate in runtime_evidence:
            compatibility = _surface_compatibility(surface_experiment, candidate)
            if compatibility["eligible"]:
                compatible_runtime_evidence.append(candidate)
            else:
                surface_quarantined_evidence.append(
                    {
                        "inst_id": candidate.get("inst_id"),
                        "venue": candidate.get("venue"),
                        **compatibility,
                    }
                )
        surface_vocabulary = _runtime_strategy_vocabulary(compatible_runtime_evidence)
        logic = _normalize_strategy_logic(original, surface_vocabulary)

        # Admission-generated ideas carry an exact instrument as evidence. Use
        # that observation to learn the runtime surface, but keep the strategy
        # scoped to the venue/surface rather than hard-coding one instrument.
        requested_inst = str(data_requirements.get("inst_id") or "").strip()
        evidence_candidates = [
            candidate
            for candidate in compatible_runtime_evidence
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
            for candidate in compatible_runtime_evidence:
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
            if not any(_candidate_field_value(candidate, field) is not None for candidate in compatible_runtime_evidence)
        ]
        capability_issues = _scope_capability_issues(
            logic, surface_vocabulary, compatible_runtime_evidence
        )
        if not _has_strategy_scope(logic):
            compile_status = "needs_contract_repair"
            status = "needs_data"
            reason = "missing_strategy_scope"
        elif unsupported_fields:
            compile_status = "needs_data"
            status = "needs_data"
            reason = "unsupported_required_fields"
        elif not compatible_runtime_evidence:
            compile_status = "needs_data"
            status = "needs_data"
            reason = (
                "no_surface_compatible_runtime_evidence"
                if runtime_evidence
                else "runtime_candidate_pool_empty"
            )
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
            "source_candidate_count": len(compatible_runtime_evidence),
            "current_candidate_count": len(candidates),
            "persisted_evidence_count": max(0, len(compatible_runtime_evidence) - len(candidates)),
            "surface_policy": surface_experiment,
            "surface_quarantined_candidate_count": len(surface_quarantined_evidence),
            "surface_quarantined_candidates": surface_quarantined_evidence[:10],
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


def _paper_route_eligible_candidates(
    candidates: list[dict],
) -> tuple[list[dict], list[dict], Counter, Counter]:
    eligible: list[dict] = []
    blocked: list[dict] = []
    missing_counts: Counter = Counter()
    blocker_counts: Counter = Counter()
    for candidate in candidates:
        annotated = dict(candidate)
        verdict = annotated.get("paper_route_eligibility")
        if not isinstance(verdict, dict):
            verdict = evaluate_route_intelligence(annotated)
        if verdict.get("applies"):
            annotated["paper_route_eligibility"] = verdict
        if not verdict.get("suppressed"):
            eligible.append(annotated)
            continue
        missing = list(verdict.get("missing_prerequisites") or [])
        reasons = list(verdict.get("blocker_reasons") or [])
        missing_counts.update(str(item) for item in missing)
        blocker_counts.update(str(item) for item in reasons)
        blocked.append(
            {
                "inst_id": candidate.get("inst_id"),
                "venue": candidate.get("venue"),
                "trade_type": candidate.get("trade_type"),
                "direction": candidate.get("direction"),
                "score": candidate.get("score"),
                "missing_prerequisites": missing,
                "blocker_reasons": reasons,
            }
        )
    return eligible, blocked, missing_counts, blocker_counts


def _observation_program_inputs(
    price_observations: dict[str, dict] | list[dict] | None,
    candidates: list[dict],
) -> list[dict]:
    """Join explicit current candidate evidence to complete price observations.

    Historical market features remain observation-native. Route, fee, and leg
    metadata are copied only when an already route-eligible source candidate
    supplies them, and cached eligibility is deliberately not propagated so
    generated candidates are checked again under their emitted direction.
    """

    candidate_by_key: dict[tuple[str, str], dict] = {}
    for candidate in candidates:
        key = (
            str(candidate.get("venue") or ""),
            str(candidate.get("inst_id") or candidate.get("instrument_id") or ""),
        )
        if key[0] and key[1] and key not in candidate_by_key:
            candidate_by_key[key] = candidate

    raw_rows = price_observations.values() if isinstance(price_observations, dict) else (price_observations or [])
    rows: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        key = (
            str(row.get("venue") or ""),
            str(row.get("inst_id") or row.get("instrument_id") or ""),
        )
        source = candidate_by_key.get(key)
        embedded = dict(row.get("candidate") or {}) if isinstance(row.get("candidate"), dict) else {}
        embedded.pop("paper_route_eligibility", None)
        if source:
            for field in PROGRAM_OBSERVATION_ENRICHMENT_FIELDS:
                if source.get(field) is not None:
                    embedded[field] = source[field]
        if embedded:
            row["candidate"] = embedded
        rows.append(row)
    return rows


def _lineage_source_health_rank_guard(
    candidate: dict,
    conn: sqlite3.Connection,
    settings: dict,
) -> tuple[dict | None, dict | None]:
    """Apply source-health evidence before a paper candidate enters a rank pool."""
    annotated = dict(candidate)
    prior_review = annotated.get("paper_lineage_source_health")
    if annotated.get("paper_lineage_source_health_rank_applied") and isinstance(
        prior_review,
        dict,
    ):
        return (annotated if prior_review.get("paper_rank_eligible") else None), None
    hydrate_paper_lineage_source_health([annotated], conn)
    review = paper_lineage_source_health_record(annotated, settings)
    if review is None:
        return annotated, None
    annotated["paper_lineage_source_health"] = review
    annotated["paper_lineage_source_health_rank_applied"] = True
    annotated["promotion_eligible"] = False
    multiplier = max(0.0, min(1.0, _as_float(review.get("paper_score_multiplier"))))
    annotated["pre_lineage_source_health_score"] = _as_float(annotated.get("score"))
    annotated["score"] = round(
        annotated["pre_lineage_source_health_score"] * multiplier,
        3,
    )
    annotated["paper_score_multiplier"] = multiplier
    annotated["paper_allocation_multiplier"] = min(
        _as_float(annotated.get("paper_allocation_multiplier"), 1.0),
        multiplier,
    )
    if not review.get("paper_rank_eligible"):
        annotated["paper_entry_blocked"] = True
        annotated["paper_fill_allowed"] = False
        return None, review
    return annotated, review


_PROGRAM_UNIVERSE_FIELDS = {
    "venues": "venue",
    "inst_ids": "inst_id",
    "trade_types": "trade_type",
    "asset_classes": "asset_class",
    "regions": "region",
    "market_types": "market_type",
    "quotes": "quote",
    "bases": "base",
}


def _runtime_universe_contract_mismatch(
    program: dict,
    observation_frames: list[dict],
    program_diagnostic: dict,
    feasibility: dict,
) -> dict | None:
    """Describe a compiled universe contract that cannot match available rows."""

    if (
        not observation_frames
        or int(feasibility.get("universe_match_count") or 0) != 0
    ):
        return None
    universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
    mismatches = []
    for plural, field in _PROGRAM_UNIVERSE_FIELDS.items():
        required_raw = universe.get(plural)
        if not required_raw:
            continue
        required = sorted(
            {
                str(value).upper()
                for value in (required_raw if isinstance(required_raw, list) else [required_raw])
            }
        )
        observed = sorted(
            {str(frame.get(field) or "<missing>").upper() for frame in observation_frames}
        )
        if set(required).intersection(observed):
            continue
        mismatches.append(
            {
                "universe_key": plural,
                "runtime_field": field,
                "required_values": required,
                "observed_values": observed[:25],
            }
        )
    if not mismatches:
        return None
    return {
        "repairable": True,
        "reason": "compiled_universe_does_not_match_available_observations",
        "observation_count": len(observation_frames),
        "universe_match_count": 0,
        "missing_features": list(program_diagnostic.get("missing_features") or []),
        "mismatches": mismatches,
        "owner_objective": "repair_runtime_contract",
    }


def _runtime_contract_program(feasibility: dict, raw_logic: dict) -> dict:
    """Keep contract diagnosis independent from transient profiler payloads."""

    profiled = feasibility.get("program")
    return profiled if isinstance(profiled, dict) and profiled else dict(raw_logic or {})


def generate_strategy_lab_candidates(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    price_observations: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("enabled", True):
        return [], {"enabled": False, "generated_candidates": 0}

    discover_signals()
    promoted_plugins = promoted_strategy_lab_ids()
    if promoted_plugins:
        conn.executemany(
            """
            update strategy_lab_experiments
            set status = 'promoted_to_code', updated_at = ?
            where strategy_lab_id = ? and status in ('promote_candidate', 'promotion_queued')
            """,
            [(_utc(), strategy_lab_id) for strategy_lab_id in promoted_plugins],
        )
        conn.commit()
    hydrate_paper_lineage_source_health(candidates, conn)
    source_vetoed_candidates: list[dict] = []
    lineage_source_health_guarded_candidates: list[dict] = []
    proxy_frontier_quarantined_candidates: list[dict] = []
    source_rank_candidates: list[dict] = []
    for candidate in candidates:
        source_veto = paper_source_veto_record(candidate, settings)
        if source_veto is not None:
            source_vetoed_candidates.append(
                {
                    "inst_id": candidate.get("inst_id"),
                    "venue": candidate.get("venue"),
                    "trade_type": candidate.get("trade_type"),
                    "direction": candidate.get("direction"),
                    "market_key": candidate.get("market_key"),
                    "strategy_lab_id": candidate.get("strategy_lab_id"),
                    "reason": source_veto["reason"],
                    "matched_on": source_veto["matched_on"],
                }
            )
            continue
        ranked_candidate, source_health = _lineage_source_health_rank_guard(
            candidate,
            conn,
            settings,
        )
        if source_health is not None:
            lineage_source_health_guarded_candidates.append(
                {
                    "inst_id": candidate.get("inst_id"),
                    "venue": candidate.get("venue"),
                    "strategy_lab_id": candidate.get("strategy_lab_id"),
                    "reason": source_health.get("reason"),
                    "action": source_health.get("action"),
                    "source_health": source_health.get("source_health"),
                }
            )
        if ranked_candidate is None:
            continue
        candidate = ranked_candidate
        transplant_review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
            candidate, settings
        )
        yahoo_lineage = paper_source_veto_record(candidate, {"mode": "paper"})
        if (
            yahoo_lineage is not None
            and transplant_review.get("applies")
            and not transplant_review.get("eligible")
        ):
            proxy_frontier_quarantined_candidates.append(
                {
                    "inst_id": candidate.get("inst_id"),
                    "venue": candidate.get("venue"),
                    "trade_type": candidate.get("trade_type"),
                    "direction": candidate.get("direction"),
                    "market_key": candidate.get("market_key"),
                    "strategy_lab_id": candidate.get("strategy_lab_id"),
                    "reason": transplant_review.get("reason"),
                    "failed_local_checks": [
                        key
                        for key, passed in (
                            transplant_review.get("local_confirmation_checks") or {}
                        ).items()
                        if not passed
                    ],
                }
            )
            continue
        source_rank_candidates.append(candidate)
    eligible_candidates, route_blocked, route_missing_counts, route_blocker_counts = (
        _paper_route_eligible_candidates(source_rank_candidates)
    )
    if cfg.get("observation_programs_enabled", True):
        observation_frames, snapshot_summary = record_feature_snapshots(
            conn,
            _observation_program_inputs(price_observations, eligible_candidates),
            settings,
        )
    else:
        observation_frames, snapshot_summary = [], {"enabled": False}
    compilation = _compile_strategy_lab_contracts(conn, eligible_candidates, observation_frames)
    all_experiments = _active_experiments(conn)
    source_vetoed_experiments: list[dict] = []
    experiments: list[dict] = []
    for experiment in all_experiments:
        source_veto = paper_source_veto_record(experiment, settings)
        if source_veto is None:
            experiments.append(experiment)
            continue
        source_vetoed_experiments.append(
            {
                "strategy_lab_id": experiment.get("strategy_lab_id"),
                "parent_strategy_lab_id": experiment.get("parent_strategy_lab_id"),
                "reason": source_veto["reason"],
                "matched_on": source_veto["matched_on"],
            }
        )
    max_total = int(cfg.get("max_candidates_per_loop", 25))
    max_per_experiment = int(cfg.get("max_candidates_per_experiment", 5))
    default_bonus = float(cfg.get("candidate_score_bonus", 1.0))
    max_bonus = float(cfg.get("max_candidate_score_bonus", 5.0))
    generated = []
    per_experiment: dict[str, int] = Counter()
    rejects: dict[str, Counter] = defaultdict(Counter)
    nearest_candidates: dict[str, list[dict]] = defaultdict(list)
    status_by_experiment: dict[str, str] = {}
    surface_quarantined_applications: list[dict] = []
    surface_quarantine_reasons: Counter = Counter()
    feasibility_by_experiment: dict[str, dict] = {}
    relaxed_children: list[dict] = []
    max_relaxations = int(
        cfg.get("adaptive_relaxation", {}).get("max_new_children_per_loop", 3)
    )

    pool = sorted(
        eligible_candidates,
        key=lambda row: (_paper_route_rank(row), -_as_float(row.get("score"))),
    )
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
        raw_logic = experiment.get("strategy_logic") or {}
        if str(raw_logic.get("type") or "") == OBSERVATION_PROGRAM:
            experiment_id = experiment["strategy_lab_id"]
            feasibility = profile_observation_program(experiment, observation_frames, settings)
            record_contract_evaluation(conn, experiment, feasibility)
            feasibility_by_experiment[experiment_id] = {
                key: value for key, value in feasibility.items() if key != "program"
            }
            relaxed_child = None
            if len(relaxed_children) < max_relaxations:
                relaxed_child = maybe_create_relaxed_child(
                    conn, experiment, feasibility, settings
                )
                if relaxed_child and relaxed_child.get("status") == "created":
                    relaxed_children.append(relaxed_child)
            remaining = max(0, max_total - len(generated))
            program_candidates, program_diagnostic = generate_program_candidates(
                experiment,
                observation_frames,
                settings,
                max_candidates=min(max_per_experiment, remaining),
            )
            runtime_contract_mismatch = _runtime_universe_contract_mismatch(
                _runtime_contract_program(feasibility, raw_logic),
                observation_frames,
                program_diagnostic,
                feasibility,
            )
            compatible_program_candidates: list[dict] = []
            for candidate in program_candidates:
                compatibility = _surface_compatibility(experiment, candidate)
                if compatibility["eligible"]:
                    annotated = dict(candidate)
                    annotated["strategy_lab_surface_policy"] = compatibility
                    annotated["source_surface"] = experiment.get("source_surface")
                    annotated["permitted_target_surface"] = list(
                        experiment.get("permitted_target_surface") or []
                    )
                    annotated["target_surface"] = compatibility["target_surface"]
                    compatible_program_candidates.append(annotated)
                    continue
                surface_quarantine_reasons[compatibility["reason"]] += 1
                rejects[experiment_id][f"surface_policy:{compatibility['reason']}"] += 1
                surface_quarantined_applications.append(
                    {
                        "strategy_lab_id": experiment_id,
                        "inst_id": candidate.get("inst_id"),
                        "venue": candidate.get("venue"),
                        **compatibility,
                    }
                )
            program_candidates = compatible_program_candidates
            admitted_program_candidates: list[dict] = []
            for candidate in program_candidates:
                transplant_subject = dict(candidate)
                transplant_subject["recommendation_lineage"] = {
                    "strategy_lab_id": experiment.get("strategy_lab_id"),
                    "parent_strategy_lab_id": experiment.get("parent_strategy_lab_id"),
                    "source_surface": experiment.get("source_surface"),
                    "strategy_logic": experiment.get("strategy_logic"),
                    "data_requirements": experiment.get("data_requirements"),
                }
                transplant_review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
                    transplant_subject, settings
                )
                yahoo_lineage = paper_source_veto_record(
                    transplant_subject, {"mode": "paper"}
                )
                if (
                    yahoo_lineage is not None
                    and transplant_review.get("applies")
                    and not transplant_review.get("eligible")
                ):
                    rejects[experiment_id]["proxy_frontier_quarantine"] += 1
                    proxy_frontier_quarantined_candidates.append(
                        {
                            "strategy_lab_id": experiment_id,
                            "inst_id": candidate.get("inst_id"),
                            "venue": candidate.get("venue"),
                            "reason": transplant_review.get("reason"),
                        }
                    )
                    continue
                admitted_program_candidates.append(candidate)
            program_candidates = admitted_program_candidates
            (
                program_candidates,
                program_route_blocked,
                program_missing_counts,
                program_blocker_counts,
            ) = _paper_route_eligible_candidates(program_candidates)
            route_blocked.extend(program_route_blocked)
            route_missing_counts.update(program_missing_counts)
            route_blocker_counts.update(program_blocker_counts)
            for reason, count in program_blocker_counts.items():
                rejects[experiment_id][f"paper_route:{reason}"] += int(count)
            generated.extend(program_candidates)
            per_experiment[experiment_id] += len(program_candidates)
            for reason, count in (program_diagnostic.get("reject_reasons") or {}).items():
                rejects[experiment_id][str(reason)] += int(count)
            if program_candidates:
                diagnostic_status = "active_testing"
            elif runtime_contract_mismatch:
                diagnostic_status = "needs_contract_revision"
            elif relaxed_child and relaxed_child.get("status") == "created":
                diagnostic_status = "needs_contract_revision"
            elif feasibility.get("feasibility_status") in {
                "missing_feature_history", "blocked_observation_safety", "missing_surface_data"
            }:
                diagnostic_status = "needs_data"
            elif feasibility.get("feasibility_status") == "unrelaxable_contract":
                diagnostic_status = "needs_contract_revision"
            elif not observation_frames or (
                program_diagnostic.get("reject_reasons")
                and set(program_diagnostic.get("reject_reasons") or {}) == {"universe_mismatch"}
            ):
                diagnostic_status = "needs_data"
            else:
                diagnostic_status = "needs_more_evidence"
            status_by_experiment[experiment_id] = diagnostic_status
            generation_diagnostic = {
                "checked_at": _utc(),
                "status": diagnostic_status,
                "logic_type": OBSERVATION_PROGRAM,
                "source_observation_count": len(observation_frames),
                "generated_candidate_count": len(program_candidates),
                "dominant_reject_reasons": dict(rejects[experiment_id].most_common(8)),
                "novelty_signature": program_diagnostic.get("novelty_signature"),
                "feasibility": {
                    key: value for key, value in feasibility.items() if key != "program"
                },
                "runtime_contract_mismatch": runtime_contract_mismatch,
                "relaxed_child": relaxed_child,
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
            continue
        logic = _normalize_strategy_logic(raw_logic, runtime_vocabulary)
        risk_gates = experiment.get("risk_gates") or {}
        bonus = max(0.0, min(max_bonus, _as_float(logic.get("score_bonus"), default_bonus)))
        edge_bonus = max(0.0, min(max_bonus, _as_float(logic.get("edge_bonus_bps"), 0.0)))
        local_limit = min(max_per_experiment, max(1, _as_int(logic.get("max_candidates_per_loop"), max_per_experiment)))
        surface_rank_pool: list[dict] = []
        for candidate in pool:
            compatibility = _surface_compatibility(experiment, candidate)
            if compatibility["eligible"]:
                annotated = dict(candidate)
                annotated["strategy_lab_surface_policy"] = compatibility
                transplant_subject = dict(annotated)
                transplant_subject["recommendation_lineage"] = {
                    "strategy_lab_id": experiment.get("strategy_lab_id"),
                    "parent_strategy_lab_id": experiment.get("parent_strategy_lab_id"),
                    "source_surface": experiment.get("source_surface"),
                    "strategy_logic": experiment.get("strategy_logic"),
                    "data_requirements": experiment.get("data_requirements"),
                }
                transplant_review = paper_only_yahoo_proxy_cross_surface_alignment_guard(
                    transplant_subject, settings
                )
                yahoo_lineage = paper_source_veto_record(
                    transplant_subject, {"mode": "paper"}
                )
                if (
                    yahoo_lineage is not None
                    and transplant_review.get("applies")
                    and not transplant_review.get("eligible")
                ):
                    rejects[experiment["strategy_lab_id"]]["proxy_frontier_quarantine"] += 1
                    proxy_frontier_quarantined_candidates.append(
                        {
                            "strategy_lab_id": experiment.get("strategy_lab_id"),
                            "inst_id": candidate.get("inst_id"),
                            "venue": candidate.get("venue"),
                            "reason": transplant_review.get("reason"),
                        }
                    )
                    continue
                surface_rank_pool.append(annotated)
                continue
            surface_quarantine_reasons[compatibility["reason"]] += 1
            rejects[experiment["strategy_lab_id"]][f"surface_policy:{compatibility['reason']}"] += 1
            surface_quarantined_applications.append(
                {
                    "strategy_lab_id": experiment["strategy_lab_id"],
                    "inst_id": candidate.get("inst_id"),
                    "venue": candidate.get("venue"),
                    **compatibility,
                }
            )
        surface_rank_pool.sort(
            key=lambda row: (_paper_route_rank(row), -_as_float(row.get("score")))
        )
        for candidate in surface_rank_pool:
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
            lab_candidate["strategy_lab_source_signal_key"] = (
                candidate.get("signal_key") or signal_key(candidate)
            )
            lab_candidate["strategy_lab_candidate"] = True
            lab_candidate["source_surface"] = experiment.get("source_surface")
            lab_candidate["permitted_target_surface"] = list(
                experiment.get("permitted_target_surface") or []
            )
            lab_candidate["target_surface"] = _candidate_target_surface(candidate)
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
            ranked_lab_candidate, source_health = _lineage_source_health_rank_guard(
                lab_candidate,
                conn,
                settings,
            )
            if source_health is not None:
                lineage_source_health_guarded_candidates.append(
                    {
                        "inst_id": lab_candidate.get("inst_id"),
                        "venue": lab_candidate.get("venue"),
                        "strategy_lab_id": lab_candidate.get("strategy_lab_id"),
                        "reason": source_health.get("reason"),
                        "action": source_health.get("action"),
                        "source_health": source_health.get("source_health"),
                    }
                )
            if ranked_lab_candidate is None:
                rejects[experiment["strategy_lab_id"]]["lineage_source_negative_edge"] += 1
                continue
            generated.append(ranked_lab_candidate)
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
        "surface_policy_enforced": True,
        "surface_quarantined_application_count": len(surface_quarantined_applications),
        "surface_quarantine_reason_counts": dict(surface_quarantine_reasons),
        "surface_quarantined_applications": surface_quarantined_applications[:20],
        "surface_quarantined_construction_count": sum(
            int(diagnostic.get("surface_quarantined_candidate_count") or 0)
            for diagnostic in (compilation.get("diagnostics") or {}).values()
        ),
        "source_vetoed_candidate_count": len(source_vetoed_candidates),
        "source_vetoed_candidates": source_vetoed_candidates[:20],
        "source_vetoed_experiment_count": len(source_vetoed_experiments),
        "source_vetoed_experiments": source_vetoed_experiments[:20],
        "lineage_source_health_guarded_candidate_count": len(
            lineage_source_health_guarded_candidates
        ),
        "lineage_source_health_guarded_candidates": lineage_source_health_guarded_candidates[:20],
        "proxy_frontier_quarantined_candidate_count": len(
            proxy_frontier_quarantined_candidates
        ),
        "proxy_frontier_quarantined_candidates": proxy_frontier_quarantined_candidates[:20],
        "paper_source_veto_recovery": paper_source_veto_recovery_status(settings),
        "route_eligible_source_candidate_count": len(eligible_candidates),
        "route_ineligible_candidate_count": len(route_blocked),
        "route_ineligible_missing_prerequisite_counts": dict(route_missing_counts),
        "route_ineligible_blocker_counts": dict(route_blocker_counts),
        "route_ineligible_examples": route_blocked[:20],
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
        "strategy_feasibility": feasibility_by_experiment,
        "adaptive_relaxed_children": relaxed_children,
        "feature_snapshots": snapshot_summary,
        "observation_program_count": sum(
            1 for experiment in experiments if str((experiment.get("strategy_logic") or {}).get("type")) == OBSERVATION_PROGRAM
        ),
        "promoted_signal_plugins": promoted_plugins,
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


def _experiment_outcomes(
    conn: sqlite3.Connection,
    strategy_lab_id: str,
    horizon: int,
    experiment: dict | None = None,
    settings: dict | None = None,
) -> dict:
    rows = conn.execute(
        """
        select p.id, p.opened_at, p.closed_at, p.venue, p.inst_id, p.direction, p.trade_type,
               p.candidate_json, p.review_json, p.entry_fee_bps, p.entry_slippage_bps,
               o.pnl_bps, o.measurement_status, o.context_json as outcome_context_json
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
    by_direction: dict[str, list[float]] = defaultdict(list)
    examples = []
    cost_audits = []
    surface_quarantined = []
    for row in rows:
        item = dict(row)
        status = str(item.get("measurement_status") or "missing")
        candidate = _json_loads(item.get("candidate_json"), {})
        if str(candidate.get("signal_stats_scope") or "direct") == "synthetic_research":
            status_counts["synthetic_research_excluded"] += 1
            continue
        if experiment is not None:
            compatibility = _surface_compatibility(experiment, candidate)
            if not compatibility["eligible"]:
                status_counts["surface_quarantined"] += 1
                surface_quarantined.append(
                    {
                        "trade_id": item["id"],
                        "venue": item.get("venue"),
                        "inst_id": item.get("inst_id"),
                        **compatibility,
                    }
                )
                continue
        status_counts[status] += 1
        review = _json_loads(item.get("review_json"), {})
        route = str(review.get("route_status") or candidate.get("route_status") or (candidate.get("execution_feasibility") or {}).get("status") or "unknown")
        route_counts[route] += 1
        if status == "valid" and item.get("pnl_bps") is not None:
            raw_pnl = _as_float(item["pnl_bps"])
            outcome_context = _json_loads(item.get("outcome_context_json"), {})
            prior_cost_audit = outcome_context.get("paper_realized_cost_audit") if isinstance(outcome_context, dict) else None
            has_entry_cost_model = bool(
                isinstance(candidate.get("paper_context_cost_gate"), dict)
                or candidate.get("estimated_round_trip_cost_bps") is not None
                or candidate.get("route_cost_bps_paper") is not None
                or candidate.get("paper_route_cost_bps") is not None
            )
            risk = (settings or {}).get("risk", {}) if isinstance(settings, dict) else {}
            charged_cost = max(0.0, _as_float(item.get("entry_fee_bps"))) + max(
                0.0, _as_float(item.get("entry_slippage_bps"))
            )
            charged_cost += max(
                0.0,
                _as_float(
                    candidate.get("estimated_fee_bps_per_side"),
                    _as_float(risk.get("taker_fee_bps_per_leg")),
                ),
            )
            charged_cost += max(
                0.0,
                _as_float(
                    candidate.get("exit_slippage_bps_estimate"),
                    _as_float(risk.get("slippage_bps_per_leg")),
                ),
            )
            cost_audit = realized_paper_cost_audit(
                candidate,
                raw_pnl,
                charged_cost_bps=charged_cost,
                settings=settings,
                already_backfilled=bool(prior_cost_audit and prior_cost_audit.get("cost_basis") == "after_modeled_context_cost")
                or not has_entry_cost_model,
            )
            pnl = _as_float(cost_audit.get("adjusted_pnl_bps"), raw_pnl)
            if cost_audit.get("backfill_applied"):
                cost_audits.append({"trade_id": item["id"], **cost_audit})
            valid.append(pnl)
            by_region[str(candidate.get("region") or "unknown")].append(pnl)
            by_venue[str(item.get("venue") or "unknown")].append(pnl)
            by_direction[str(item.get("direction") or candidate.get("direction") or "unknown")].append(pnl)
            if len(examples) < 10:
                examples.append(
                    {
                        "trade_id": item["id"],
                        "venue": item["venue"],
                        "inst_id": item["inst_id"],
                        "direction": item["direction"],
                        "pnl_bps": round(pnl, 3),
                        "raw_pnl_bps": round(raw_pnl, 3),
                        "realized_cost_backfill_bps": cost_audit.get("realized_cost_backfill_bps"),
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
        "by_direction": {key: _pnl_stats(value) for key, value in by_direction.items()},
        "examples": examples,
        "surface_quarantined_count": len(surface_quarantined),
        "surface_quarantined_examples": surface_quarantined[:10],
        "realized_cost_backfill": {
            "paper_only": True,
            "applied_count": len(cost_audits),
            "total_backfill_bps": round(
                sum(_as_float(item.get("realized_cost_backfill_bps")) for item in cost_audits), 3
            ),
            "examples": cost_audits[:10],
        },
    }


def _rules(settings: dict, experiment: dict) -> dict:
    defaults = settings.get("strategy_lab", {})
    custom = experiment.get("promotion_rules") or {}
    rules = {
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
    for key, cast in (
        ("min_labels_per_direction", int),
        ("min_avg_pnl_bps_per_direction", float),
        ("min_win_rate_per_direction", float),
    ):
        value = custom.get(key)
        rules[key] = cast(value) if value is not None else None
    return rules


def _promotion_directions(experiment: dict) -> list[str]:
    custom = experiment.get("promotion_rules") or {}
    explicit = custom.get("required_directions")
    if isinstance(explicit, str):
        explicit = [piece.strip() for piece in explicit.replace(";", ",").split(",")]
    if isinstance(explicit, (list, tuple, set)):
        directions = _unique([str(item).strip() for item in explicit if str(item).strip()])
        if directions:
            return directions

    logic = experiment.get("strategy_logic") or {}
    configured = _as_list(logic.get("directions") or logic.get("allowed_directions"))
    if configured:
        return _unique([str(item).strip() for item in configured if str(item).strip()])
    if str(logic.get("type") or "") != OBSERVATION_PROGRAM:
        direction = str(logic.get("direction") or "").strip()
        return [direction] if direction else []

    surface = str(logic.get("route_surface") or "auto").lower()
    suffixes = {
        "proxy": ("long_proxy", "short_proxy"),
        "spot": ("long_frontier_spot", "short_frontier_spot"),
        "perp": ("long_frontier_perp", "short_frontier_perp"),
        "prediction": ("yes", "no"),
    }
    long_direction, short_direction = suffixes.get(surface, ("long", "short"))
    fixed = str(logic.get("direction") or "").lower()
    if fixed == "long":
        return [long_direction]
    if fixed == "short":
        return [short_direction]
    directions = []
    if str(logic.get("long_expression") or "False") != "False":
        directions.append(long_direction)
    if str(logic.get("short_expression") or "False") != "False":
        directions.append(short_direction)
    return directions


def _direction_promotion_gate(experiment: dict, outcomes: dict, rules: dict) -> dict:
    thresholds = {
        "min_labels": rules.get("min_labels_per_direction"),
        "min_avg_pnl_bps": rules.get("min_avg_pnl_bps_per_direction"),
        "min_win_rate": rules.get("min_win_rate_per_direction"),
    }
    enabled = any(value is not None for value in thresholds.values())
    required = _promotion_directions(experiment) if enabled else []
    checks = {}
    for direction in required:
        metrics = (outcomes.get("by_direction") or {}).get(direction) or _pnl_stats([])
        failures = []
        if thresholds["min_labels"] is not None and int(metrics.get("count") or 0) < int(thresholds["min_labels"]):
            failures.append("min_labels")
        if thresholds["min_avg_pnl_bps"] is not None and (
            metrics.get("avg_pnl_bps") is None
            or float(metrics["avg_pnl_bps"]) < float(thresholds["min_avg_pnl_bps"])
        ):
            failures.append("min_avg_pnl_bps")
        if thresholds["min_win_rate"] is not None and (
            metrics.get("win_rate") is None
            or float(metrics["win_rate"]) < float(thresholds["min_win_rate"])
        ):
            failures.append("min_win_rate")
        checks[direction] = {
            "passed": not failures,
            "failed_thresholds": failures,
            "metrics": metrics,
        }
    return {
        "enabled": enabled,
        "passed": not enabled or (bool(required) and all(item["passed"] for item in checks.values())),
        "required_directions": required,
        "thresholds": thresholds,
        "checks": checks,
    }


def _queue_promotion(
    conn: sqlite3.Connection,
    experiment: dict,
    evaluation: dict,
    rules: dict,
    settings: dict | None = None,
) -> str | None:
    if paper_source_veto_record(experiment, settings) is not None:
        return None
    proposal_id = "strategy_lab_promotion_" + hashlib.sha256(
        f"{experiment['strategy_lab_id']}:{experiment['version']}".encode("utf-8")
    ).hexdigest()[:16]
    logic = experiment.get("strategy_logic", {})
    is_observation_program = str(logic.get("type") or "") == OBSERVATION_PROGRAM
    signal_slug = _slug(str(experiment["strategy_lab_id"]))
    target_module = f"src/signals/generated/{signal_slug}.py"
    expected_files = (
        [target_module, "tests/test_generated_strategy_parity.py"]
        if is_observation_program
        else ["src/strategy_lab.py", "src/radar_loop.py", "tests/test_strategy_lab.py"]
    )
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
                "strategy_logic": logic,
                "risk_gates": experiment.get("risk_gates", {}),
                "source_surface": experiment.get("source_surface"),
                "permitted_target_surface": experiment.get("permitted_target_surface", []),
            },
            "promotion_target": {
                "module": target_module if is_observation_program else None,
                "signal_id": f"strategy_lab_{signal_slug}",
                "novelty_signature": experiment.get("novelty_signature") or (
                    novelty_signature(logic) if is_observation_program else None
                ),
                "registry": "signals.registry.discover_signals",
                "parity_requirement": (
                    "The generated plugin must reproduce strategy_program.generate_program_candidates "
                    "for identical feature fixtures before promotion."
                    if is_observation_program
                    else "Preserve the accepted candidate-filter contract."
                ),
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
            "expected_files": expected_files,
            "tests_to_run": [
                "python -m unittest tests.test_generated_strategy_parity tests.test_strategy_program"
                if is_observation_program
                else "python -m unittest tests.test_strategy_lab"
            ],
            "runtime_integration": {
                "entrypoint_file": "src/signals/registry.py" if is_observation_program else "src/radar_loop.py",
                "entrypoint_symbol": "discover_signals" if is_observation_program else "run_once",
                "invocation_path": (
                    "radar_loop.run_once -> signals.registry.discover_signals -> generated signal plugin"
                    if is_observation_program
                    else "radar_loop.run_once -> strategy_lab.generate_strategy_lab_candidates"
                ),
                "test_file": "tests/test_generated_strategy_parity.py" if is_observation_program else "tests/test_strategy_lab.py",
                "behavioral_test": (
                    "Generated plugin candidates equal the observation-program interpreter candidates on fixtures."
                    if is_observation_program
                    else "Promoted candidate behavior remains route-compatible."
                ),
            },
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


def _maybe_split_children(
    conn: sqlite3.Connection,
    experiment: dict,
    outcomes: dict,
    settings: dict | None = None,
) -> list[str]:
    created = []
    if paper_source_veto_record(experiment, settings) is not None:
        return created
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
                    promotion_rules_json, source_surface, permitted_target_surfaces_json, surface_policy_json,
                    source_agent, source_recommendation_id,
                    created_at, updated_at
                ) values (?, ?, ?, ?, 'active_testing', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    experiment.get("source_surface"),
                    json.dumps(experiment.get("permitted_target_surface") or [], sort_keys=True),
                    json.dumps(experiment.get("surface_policy") or {}, sort_keys=True),
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
        source_veto = paper_source_veto_record(experiment, settings)
        if source_veto is not None:
            evaluations.append(
                {
                    "strategy_lab_id": experiment["strategy_lab_id"],
                    "experiment_type": experiment.get("experiment_type", DEFAULT_EXPERIMENT_TYPE),
                    "status": experiment.get("status"),
                    "decision": "paper_source_family_veto",
                    "source_veto": source_veto,
                }
            )
            continue
        rules = _rules(settings, experiment)
        outcomes = _experiment_outcomes(
            conn,
            experiment["strategy_lab_id"],
            rules["horizon_minutes"],
            experiment,
            settings,
        )
        metrics = outcomes["metrics"]
        count = int(metrics.get("count") or 0)
        avg = metrics.get("avg_pnl_bps")
        win_rate = metrics.get("win_rate")
        worst_decile = metrics.get("worst_decile_pnl_bps")
        active_hours = _age_hours(experiment.get("created_at"))
        blocked_routes = int(outcomes.get("route_status_counts", {}).get("blocked", 0))
        direction_promotion = _direction_promotion_gate(experiment, outcomes, rules)
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
            and direction_promotion["passed"]
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
                promotion_id = _queue_promotion(conn, experiment, outcomes, rules, settings)
                status = "promotion_queued"
                decision = "promotion_queued" if promotion_id else "promotion_already_queued"
        elif retire_ready:
            status = "retired_bad_evidence"
            passes = 0
            decision = "retired_bad_evidence"
        elif expand_ready:
            decision = "expand_testing_modestly"
        elif count >= rules["retire_min_labels"]:
            children = _maybe_split_children(conn, experiment, outcomes, settings)
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
            "direction_promotion": direction_promotion,
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
                "realized_cost_backfill": outcomes["realized_cost_backfill"],
                "direction_promotion": direction_promotion,
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
               compile_diagnostics_json, runtime_schema_fingerprint, compile_attempts, last_compiled_at,
               novelty_signature, novelty_status, novelty_details_json,
               source_surface, permitted_target_surfaces_json, surface_policy_json
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
        item["novelty_details"] = _json_loads(item.pop("novelty_details_json"), {})
        item["permitted_target_surface"] = _json_loads(
            item.pop("permitted_target_surfaces_json"), []
        )
        item["surface_policy"] = _json_loads(item.pop("surface_policy_json"), {})
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
    novelty_status_counts = {
        (row["novelty_status"] or "unassessed"): int(row["n"])
        for row in conn.execute(
            """
            select novelty_status, count(*) as n
            from strategy_lab_experiments
            group by novelty_status
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
        "novelty_status_counts": novelty_status_counts,
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
        "surface_quarantine_review": [
            {
                "strategy_lab_id": item["strategy_lab_id"],
                "status": item["status"],
                "source_surface": item.get("source_surface"),
                "permitted_target_surface": item.get("permitted_target_surface", []),
                "surface_policy": item.get("surface_policy", {}),
            }
            for item in items
            if item.get("status") == "quarantined_surface_policy"
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
        f"- Program novelty: `{summary.get('novelty_status_counts', {})}`",
        f"- Candidates generated this loop: `{(generation or {}).get('generated_candidates', 0)}`",
        f"- Route-ineligible candidates filtered: `{(generation or {}).get('route_ineligible_candidate_count', 0)}`",
        f"- Source-vetoed candidates: `{(generation or {}).get('source_vetoed_candidate_count', 0)}`",
        f"- Source-vetoed experiments: `{(generation or {}).get('source_vetoed_experiment_count', 0)}`",
        f"- Missing route prerequisites: `{(generation or {}).get('route_ineligible_missing_prerequisite_counts', {})}`",
        f"- Route eligibility blockers: `{(generation or {}).get('route_ineligible_blocker_counts', {})}`",
        f"- Feature snapshots: `{(generation or {}).get('feature_snapshots', {})}`",
        f"- Adaptive relaxed children this loop: `{len((generation or {}).get('adaptive_relaxed_children', []))}`",
        "",
        "## Market Strategy Experiments",
        "",
    ]
    if not summary.get("recent_market_strategies"):
        lines.append("No market-strategy experiments yet.")
    for item in summary.get("recent_market_strategies", [])[:20]:
        latest = item.get("evaluation", {})
        decision = latest.get("decision")
        outcomes = latest.get("outcomes") or {}
        metrics = outcomes.get("metrics") or {}
        cost_backfill = outcomes.get("realized_cost_backfill") or {}
        lines.append(
            f"- `{item['strategy_lab_id']}` type=`{item.get('experiment_type')}` "
            f"status=`{item['status']}` decision=`{decision}` "
            f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
            f"win=`{metrics.get('win_rate')}` cost_backfills=`{cost_backfill.get('applied_count', 0)}` "
            f"hypothesis={item.get('hypothesis')}"
        )
    lines.extend(["", "## Contract Feasibility", ""])
    feasibility = (generation or {}).get("strategy_feasibility", {})
    if not feasibility:
        lines.append("No observation-program feasibility profiles were produced this loop.")
    for strategy_lab_id, profile in list(feasibility.items())[:30]:
        lines.append(
            f"- `{strategy_lab_id}` status=`{profile.get('feasibility_status')}` "
            f"observations=`{profile.get('universe_match_count', 0)}` candidates=`{profile.get('candidate_count', 0)}` "
            f"entry_rate=`{profile.get('entry_pass_rate', 0)}` relaxable=`{bool((profile.get('relaxation') or {}).get('changes'))}`"
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
        outcomes = latest.get("outcomes") or {}
        metrics = outcomes.get("metrics") or {}
        cost_backfill = outcomes.get("realized_cost_backfill") or {}
        lines.append(
            f"- `{item['strategy_lab_id']}` type=`{item.get('experiment_type')}` "
            f"status=`{item['status']}` decision=`{decision}` "
            f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
            f"win=`{metrics.get('win_rate')}` cost_backfills=`{cost_backfill.get('applied_count', 0)}` "
            f"hypothesis={item.get('hypothesis')}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
