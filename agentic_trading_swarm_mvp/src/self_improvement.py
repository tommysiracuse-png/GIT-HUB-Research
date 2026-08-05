"""Autonomous paper-only improvement executor.

This consumes the build/research tasks already produced by the LLM swarm and
turns safe classes of recommendations into bounded, reversible experiments.
It never enables live trading, touches credentials, installs packages, or
changes startup behavior.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shlex
import sqlite3

from paper_exploration import exploration_enabled
from storage import (
    RUNS_DIR,
    active_signal_policies,
    add_adapter_spec,
    add_growth_experiment,
    add_memory_fact,
    add_route_probe_task,
    add_self_improvement_experiment,
    add_signal_policy,
    expire_signal_policies,
    llm_recommendations_for_auto_execution,
    open_adapter_specs,
    open_route_probe_tasks,
    open_self_improvement_experiments,
    record_policy_application,
    record_policy_open,
    update_experiment_evaluation,
    update_llm_recommendation_status,
)
from signal_redesign import create_proposed_variant
from strategy_lab import (
    ingest_strategy_lab_recommendation as _strategy_lab_ingest_strategy_lab_recommendation,
    strategy_lab_summary,
)
from code_evolution import (
    code_evolution_summary,
    evaluate_code_evolution,
    process_code_change_recommendation as _process_code_change_recommendation,
    write_code_evolution_reports,
)
from self_improvement_open_pack import IMPLEMENTED_STATUS as OPEN_PACK_IMPLEMENTED_STATUS
from self_improvement_open_pack import is_duplicate_open_pack_text
from recommendation_registry import (
    backfill_open_artifacts,
    bind_artifact,
    claim_topic,
    reconcile_deployed_artifacts,
    registry_summary,
)
from strategy_implementation_owner import (
    enqueue_recommendation as enqueue_strategy_owner_recommendation,
    summary as strategy_owner_summary,
)


ACTIVE_POLICIES_JSON = RUNS_DIR / "active_signal_policies.json"
REPORT_JSON = RUNS_DIR / "self_improvement_report.json"
REPORT_MD = RUNS_DIR / "self_improvement_report.md"
TIMELINE_JSONL = RUNS_DIR / "self_improvement_timeline.jsonl"
RECOMMENDATION_ALLOWED_PATH_PREFIXES = ("src/", "tests/", "config/", "docs/")
RECOMMENDATION_ALLOWED_FILES = {
    "README.md",
    "COST_AWARE_SWARM.md",
    "LLM_AGENT_BRIDGE.md",
    "requirements-autonomous.txt",
    "requirements-llm.txt",
}
RECOMMENDATION_REJECTION_TTL_SECONDS = 6 * 3600
_CONSUMER_REJECTION_CACHE: dict[str, dt.datetime] = {}
_SAFE_CODE_EVOLUTION_TEST_FALLBACK = (
    "python -m unittest tests.test_code_evolution_runner",
    "python -m unittest tests.test_test_command_policy",
    "python -m unittest discover -s tests -p test_*self_improvement*.py",
)


def _utc_now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _proposal_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=repr)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


_STRATEGY_LAB_QUALITY_GATE_TERMS = (
    "quality gate",
    "quality_gate",
    "quality_gate_experiment",
    "risk filter",
    "risk_filter",
    "entry gate",
    "freshness",
    "spread",
    "liquidity",
    "confidence",
)
_STRATEGY_LAB_DISALLOWED_LIVE_TERMS = (
    "live trading",
    "live execution",
    "send order",
    "place order",
    "broker write",
    "api key",
    "credentials",
    "private key",
)


def _merge_mapping_values(*values: object) -> dict:
    merged: dict = {}
    for value in values:
        if isinstance(value, dict):
            merged.update(value)
    return merged


def _first_present(mapping: dict, *keys: str) -> object:
    for key in keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if isinstance(value, str):
            if value.strip():
                return value.strip()
            continue
        if value is not None:
            return value
    return None


def _coerce_number(value: object, *, integer: bool = False) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if integer:
        return int(round(number))
    return number


def _coerce_string_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    normalized_items: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip().lower()
        if not text:
            continue
        text = text.replace("-", "_").replace(" ", "_")
        if text in seen:
            continue
        seen.add(text)
        normalized_items.append(text)
    return normalized_items


def _command_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)]


def _clean_command_text(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    text = text.strip().strip("`").strip()
    text = re.sub(r"^\s*(?:\d+[.)]\s*|[-*]\s*)", "", text)
    text = re.sub(
        r"^\s*(?:command|cmd|tests?_to_run|tests?|run)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


def _looks_like_python_executable(token: str) -> bool:
    base = str(token or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base in {"python", "python.exe", "py", "py.exe"} or base.startswith("python3")


def _normalize_unittest_target_token(token: str) -> str:
    text = str(token or "").strip()
    if not text:
        return text
    if text.startswith("-") or text == "discover":
        return text
    if text.startswith("./"):
        text = text[2:]
    candidate = text.replace("\\", "/")
    if candidate.endswith(".py") and not any(ch in candidate for ch in "*?[]"):
        candidate = candidate[:-3].strip("/")
        if candidate:
            return candidate.replace("/", ".")
    return text


def _normalize_unittest_command(command: object) -> str | None:
    if isinstance(command, (list, tuple)):
        tokens = [str(part).strip() for part in command if str(part).strip()]
    else:
        text = _clean_command_text(str(command or ""))
        if not text:
            return None
        if any(operator in text for operator in ("&&", "||", ";", "|", ">", "<")):
            return None
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            return None
    if len(tokens) < 3 or not _looks_like_python_executable(tokens[0]):
        return None
    if tokens[1] != "-m" or tokens[2] != "unittest":
        return None
    normalized_tokens = ["python", "-m", "unittest"]
    for token in tokens[3:]:
        if any(operator in token for operator in ("&&", "||", ";", "|", ">", "<")):
            return None
        normalized_tokens.append(_normalize_unittest_target_token(token))
    return " ".join(part for part in normalized_tokens if part)


def _normalize_code_change_test_commands(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload

    normalized_payload = dict(payload)
    requested_commands: list[str] = []
    source_keys: list[str] = []
    for key in ("tests_to_run", "test_command"):
        values = _command_list(payload.get(key))
        if values:
            source_keys.append(key)
            requested_commands.extend(values)

    for parent_key in ("code_change", "autonomous_plan"):
        parent = payload.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for child_key in ("tests_to_run", "test_command"):
            values = _command_list(parent.get(child_key))
            if values:
                source_keys.append(f"{parent_key}.{child_key}")
                requested_commands.extend(values)

    requested_commands = [str(command).strip() for command in requested_commands if str(command).strip()]
    normalized_commands: list[str] = []
    rejected_commands: list[str] = []
    for command in requested_commands:
        normalized_command = _normalize_unittest_command(command)
        if normalized_command:
            if normalized_command not in normalized_commands:
                normalized_commands.append(normalized_command)
            continue
        rejected_commands.append(command)

    used_fallback = not normalized_commands
    if used_fallback:
        normalized_commands = list(_SAFE_CODE_EVOLUTION_TEST_FALLBACK)

    consumer_validation = (
        dict(normalized_payload.get("consumer_validation") or {})
        if isinstance(normalized_payload.get("consumer_validation"), dict)
        else {}
    )
    consumer_validation["normalized_test_commands"] = True

    test_command_policy = (
        dict(normalized_payload.get("test_command_policy") or {})
        if isinstance(normalized_payload.get("test_command_policy"), dict)
        else {}
    )
    test_command_policy.update(
        {
            "original_test_commands": requested_commands,
            "normalized_test_commands": list(normalized_commands),
            "rejected_test_commands": rejected_commands,
            "source_keys": source_keys,
            "used_fallback": used_fallback,
            "fallback_reason": "missing_or_invalid_unittest_command" if used_fallback else None,
            "repaired": bool(
                used_fallback
                or rejected_commands
                or normalized_commands != requested_commands
            ),
        }
    )

    normalized_payload["consumer_validation"] = consumer_validation
    normalized_payload["test_command_policy"] = test_command_policy
    normalized_payload["tests_to_run"] = list(normalized_commands)
    normalized_payload["test_command"] = normalized_commands[0]

    for parent_key in ("code_change", "autonomous_plan"):
        parent = normalized_payload.get(parent_key)
        if not isinstance(parent, dict):
            continue
        parent_copy = dict(parent)
        parent_copy["tests_to_run"] = list(normalized_commands)
        parent_copy["test_command"] = normalized_commands[0]
        normalized_payload[parent_key] = parent_copy

    return normalized_payload


def process_code_change_recommendation(conn: sqlite3.Connection, rec: dict, settings: dict):
    normalized_rec = dict(rec)
    normalized_rec["payload"] = _normalize_code_change_test_commands(dict(rec.get("payload") or {}))
    return _process_code_change_recommendation(conn, normalized_rec, settings)


def _canonical_strategy_lab_key(payload: dict) -> str:
    entry_gates = payload.get("entry_gates") if isinstance(payload.get("entry_gates"), dict) else {}
    exclusions = _coerce_string_list(payload.get("excluded_modes"))
    parts = [
        str(payload.get("experiment_type") or ""),
        str(payload.get("market_key") or ""),
        str(payload.get("signal_key") or ""),
        ",".join(_coerce_string_list(payload.get("trade_types"))),
        ",".join(_coerce_string_list(payload.get("allowed_directions"))),
        f"age={entry_gates.get('max_signal_age_seconds', '')}",
        f"spread={entry_gates.get('max_spread_bps', '')}",
        f"liq={entry_gates.get('min_liquidity_usd', '')}",
        f"liq_score={entry_gates.get('min_liquidity_score', '')}",
        f"conf={entry_gates.get('min_confidence', '')}",
        f"carry={bool(entry_gates.get('require_carry_alignment'))}",
        f"exclude={','.join(exclusions)}",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _funding_capture_directions(scope: dict) -> list[str]:
    mode = str(_first_present(scope, "mode", "direction_mode", "strategy_mode") or "").lower()
    requested = _coerce_string_list(
        _first_present(scope, "allowed_directions", "directions", "trade_directions")
    )
    requested.extend(_coerce_string_list(_first_present(scope, "trade_types", "allowed_trade_types")))
    requested.append(mode)
    text = " ".join(requested)
    if "long" in text and ("only" in text or "short" not in text):
        return ["funding_capture_long_perp"]
    if "short" in text and ("only" in text or "long" not in text):
        return ["funding_capture_short_perp"]

    directions: list[str] = []
    for item in requested:
        if item in {"long", "funding_capture_long_perp"}:
            directions.append("funding_capture_long_perp")
        elif item in {"short", "funding_capture_short_perp"}:
            directions.append("funding_capture_short_perp")
    return list(dict.fromkeys(directions)) or [
        "funding_capture_long_perp",
        "funding_capture_short_perp",
    ]


def _normalize_strategy_lab_recommendation_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    full_text = f"{_payload_text(payload)} {json.dumps(payload, sort_keys=True, default=repr)}".lower()
    if _contains_any(full_text, _STRATEGY_LAB_DISALLOWED_LIVE_TERMS):
        return payload
    action = str(payload.get("action") or "")
    if action != "propose_strategy_lab_experiment" and "strategy lab" not in full_text:
        return payload
    if not _contains_any(full_text, _STRATEGY_LAB_QUALITY_GATE_TERMS):
        return payload
    if not (_contains_any(full_text, ("okx",)) and _contains_any(full_text, ("funding", "funding_capture", "perp", "basis"))):
        return payload

    code_change = payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}
    plan = payload.get("autonomous_plan") if isinstance(payload.get("autonomous_plan"), dict) else {}
    variant = payload.get("variant_config") if isinstance(payload.get("variant_config"), dict) else {}
    variant_filters = variant.get("filters") if isinstance(variant.get("filters"), dict) else {}
    scope = _merge_mapping_values(
        plan,
        code_change,
        payload.get("experiment"),
        payload.get("policy"),
        payload.get("filters"),
        payload.get("gates"),
        payload.get("scope"),
        payload.get("target"),
        variant,
        variant_filters,
        payload,
    )

    max_signal_age_seconds = _coerce_number(
        _first_present(
            scope,
            "max_signal_age_seconds",
            "signal_max_age_seconds",
            "max_age_seconds",
            "freshness_horizon_seconds",
            "max_signal_age_s",
        ),
        integer=True,
    )
    max_spread_bps = _coerce_number(
        _first_present(
            scope,
            "max_spread_bps",
            "spread_bps_max",
            "max_entry_spread_bps",
            "spread_cap_bps",
        )
    )
    min_liquidity_usd = _coerce_number(
        _first_present(
            scope,
            "min_liquidity_usd",
            "liquidity_floor_usd",
            "min_depth_usd",
            "min_notional_usd",
        )
    )
    min_confidence = _coerce_number(
        _first_present(
            scope,
            "min_confidence",
            "confidence_floor",
            "min_emit_confidence",
            "decayed_confidence_min",
        )
    )
    min_liquidity_score = _coerce_number(
        _first_present(
            scope,
            "min_liquidity_score",
            "liquidity_score_floor",
        )
    )

    entry_gates = {
        "max_signal_age_seconds": max_signal_age_seconds if max_signal_age_seconds is not None else 900,
        "max_spread_bps": max_spread_bps if max_spread_bps is not None else 8.0,
        "min_liquidity_usd": min_liquidity_usd if min_liquidity_usd is not None else 25000.0,
        "min_liquidity_score": min_liquidity_score if min_liquidity_score is not None else 0.35,
        "min_confidence": min_confidence if min_confidence is not None else 0.55,
        "require_carry_alignment": bool(
            _first_present(
                scope,
                "require_carry_alignment",
                "carry_alignment_required",
                "funding_alignment_required",
                "require_funding_carry_alignment",
            )
            if _first_present(
                scope,
                "require_carry_alignment",
                "carry_alignment_required",
                "funding_alignment_required",
                "require_funding_carry_alignment",
            )
            is not None
            else True
        ),
    }
    excluded_modes = _coerce_string_list(
        _first_present(
            scope,
            "excluded_modes",
            "exclude_modes",
            "exclusions",
            "disabled_variants",
            "disallowed_modes",
        )
    )
    if not excluded_modes:
        excluded_modes = ["basis_mean_reversion", "spot_leg", "spot_carry"]
    allowed_directions = _funding_capture_directions(scope)
    venue = str(_first_present(scope, "venue", "exchange") or "OKX").upper()
    if "OKX" not in venue:
        venue = "OKX"
    strategy_logic = {
        "type": "candidate_filter",
        "venues": [venue],
        "trade_types": ["perp_funding_basis"],
        "directions": allowed_directions,
        "required_fields": [
            "funding_bps",
            "spread_bps",
            "liquidity_score",
            "seen_at",
        ],
        "max_spread_bps": entry_gates["max_spread_bps"],
        "min_liquidity_score": entry_gates["min_liquidity_score"],
        "min_score": entry_gates["min_confidence"],
        "max_stale_minutes": round(float(entry_gates["max_signal_age_seconds"]) / 60.0, 3),
        "require_route_feasible": True,
    }
    if entry_gates["min_liquidity_usd"] is not None:
        strategy_logic["required_fields"].append("quote_volume_24h")
        strategy_logic["min_field_values"] = {
            "quote_volume_24h": entry_gates["min_liquidity_usd"],
        }
    if entry_gates["require_carry_alignment"]:
        strategy_logic["required_fields"].append("carry_alignment_status")
        strategy_logic["allowed_field_values"] = {
            "carry_alignment_status": ["carry_aligned_positive"],
        }
    evaluation = variant.get("evaluation") if isinstance(variant.get("evaluation"), dict) else {}
    review_labels = _coerce_number(
        _first_present(evaluation, "minimum_closed_trades_for_review", "min_closed_trades"),
        integer=True,
    )

    normalized = dict(payload)
    normalized["canonical_key"] = _canonical_strategy_lab_key(
        {
            "experiment_type": "market_strategy",
            "market_key": "okx_perp_funding_basis",
            "signal_key": "okx_funding_capture",
            "trade_types": ["perp_funding_basis"],
            "allowed_directions": allowed_directions,
            "entry_gates": entry_gates,
            "excluded_modes": excluded_modes,
        }
    )
    strategy_lab_id = f"okx_funding_capture_quality_gate_{normalized['canonical_key'][:12]}"
    normalized.update(
        {
            "action": "propose_strategy_lab_experiment",
            "paper_only": True,
            "runtime_mode": "paper_only",
            "experiment_type": "market_strategy",
            "market_key": "okx_perp_funding_basis",
            "signal_key": "okx_funding_capture",
            "strategy_family": str(_first_present(scope, "strategy_family", "family") or "funding_capture"),
            "source_surface": "perp_funding_basis",
            "permitted_target_surface": ["perp_funding_basis"],
            "venue": venue,
            "trade_types": ["perp_funding_basis"],
            "allowed_directions": allowed_directions,
            "entry_gates": entry_gates,
            "excluded_modes": excluded_modes,
            "scope": {
                "venue": venue,
                "market_key": "okx_perp_funding_basis",
                "signal_key": "okx_funding_capture",
                "trade_types": ["perp_funding_basis"],
            },
            "strategy_lab_experiment": {
                "strategy_lab_id": strategy_lab_id,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": str(
                    payload.get("rationale")
                    or payload.get("title")
                    or "Tighter OKX funding-capture quality gates improve paper outcomes."
                ),
                "source_surface": "perp_funding_basis",
                "permitted_target_surface": ["perp_funding_basis"],
                "strategy_logic": strategy_logic,
                "data_requirements": {
                    "source_trade_type": "perp_funding_basis",
                    "paper_only": True,
                    "excluded_modes": excluded_modes,
                    "requested_min_liquidity_usd": entry_gates["min_liquidity_usd"],
                },
                "risk_gates": {
                    key: strategy_logic[key]
                    for key in (
                        "max_spread_bps",
                        "min_liquidity_score",
                        "min_score",
                        "max_stale_minutes",
                        "require_route_feasible",
                        "min_field_values",
                        "allowed_field_values",
                    )
                    if key in strategy_logic
                },
                "promotion_rules": (
                    {"expand_min_labels": int(review_labels)}
                    if review_labels is not None and review_labels > 0
                    else {}
                ),
            },
            "consumer_validation": {
                "normalized_strategy_lab_packet": True,
                "normalization_family": "okx_funding_capture_quality_gate",
                "normalization_audit_version": 2,
            },
        }
    )
    return normalized


def ingest_strategy_lab_recommendation(
    conn: sqlite3.Connection,
    rec: dict,
    settings: dict | None = None,
):
    normalized_rec = dict(rec)
    normalized_rec["payload"] = _normalize_strategy_lab_recommendation_payload(
        dict(rec.get("payload") or {})
    )
    return _strategy_lab_ingest_strategy_lab_recommendation(conn, normalized_rec, settings)

IMPLEMENTED_MANUAL_STATUSES = {
    "route_requirements": ("implemented_route_requirements", ("improvement_tasks", "route_probe_tasks")),
    "frontier_crypto_adapter": ("implemented_frontier_crypto_adapter", ("improvement_tasks", "adapter_specs")),
    "failure_diagnostics": ("implemented_failure_diagnostics", ("improvement_tasks", "adapter_specs")),
    "signal_redesign": ("implemented_signal_redesign", ("improvement_tasks", "adapter_specs")),
    "frontier_data_quality": (
        "implemented_frontier_data_quality",
        ("improvement_tasks", "adapter_specs"),
    ),
    "okx_basis_signal_research": (
        "implemented_okx_basis_signal_research",
        ("adapter_specs",),
    ),
    "regional_frontier_data": (
        "implemented_regional_frontier_data",
        ("improvement_tasks", "adapter_specs"),
    ),
    "frontier_systemic_redesign": (
        "implemented_frontier_systemic_redesign",
        ("improvement_tasks", "growth_experiments"),
    ),
    "okx_reliable_outcomes": (
        "implemented_okx_reliable_outcomes",
        ("improvement_tasks",),
    ),
    "strategy_reliability_pack": (
        "implemented_strategy_reliability_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "self_improvement_open_pack": (
        OPEN_PACK_IMPLEMENTED_STATUS,
        ("improvement_tasks", "growth_experiments"),
    ),
    "regional_fx_frontier_prediction_pack": (
        "implemented_regional_fx_frontier_prediction_pack",
        ("route_probe_tasks", "improvement_tasks", "adapter_specs"),
    ),
    "global_market_discovery_scan": (
        "implemented_global_market_discovery_scan",
        ("adapter_specs", "growth_experiments", "route_probe_tasks", "market_hunter_directives"),
    ),
}

GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS = (
    "proxy map",
    "proxy-priced",
    "bybit",
    "bitso",
    "valr",
    "luno",
    "b3",
    "cme group",
    "eurex",
    "national stock exchange of india",
    "japan exchange group",
    "frankfurter",
    "ecb reference fx",
    "manifold markets",
    "finra trace",
    "pinnacle api",
    "london stock exchange",
    "tmx group",
    "hong kong exchanges",
    "euronext",
    "taiwan stock exchange",
    "korea exchange",
    "bolsa mexicana",
    "australian securities exchange",
    "six swiss exchange",
    "cboe global markets",
    "johannesburg stock exchange",
    "singapore exchange",
    "intercontinental exchange",
    "saudi exchange",
    "london metal exchange",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _payload_text(payload: dict) -> str:
    parts = []
    for key in ("title", "rationale", "proposed_change", "action", "market_key", "signal_key"):
        value = payload.get(key, "")
        if isinstance(value, (dict, list)):
            parts.append(json.dumps(value, sort_keys=True, default=repr))
        else:
            parts.append(str(value))
    code_change = payload.get("code_change")
    if isinstance(code_change, dict):
        parts.append(json.dumps(code_change, sort_keys=True, default=repr))
    return " ".join(parts).lower()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _term_score(text: str, terms: tuple[str, ...], *, cap: int | None = None) -> int:
    score = sum(1 for term in terms if term in text)
    return min(score, cap) if cap is not None else score


def _market_data_adapter_score(payload: dict, text: str) -> int:
    action = str(payload.get("action") or "")
    score = 0
    if action == "request_market_adapter":
        score += 5
    elif action == "request_data_source":
        score += 4
    elif action == "propose_build_task":
        score += 1
    score += 2 * _term_score(
        text,
        (
            "adapter",
            "parser",
            "scanner",
            "ingest",
            "ingestion",
            "collector",
            "normalize",
            "normaliz",
        ),
        cap=3,
    )
    score += _term_score(
        text,
        (
            "market data",
            "public data",
            "public no-key",
            "no-key",
            "official docs",
            "api docs",
            "endpoint",
            "rest",
            "websocket",
            "feed",
            "ticker",
            "quote",
            "bid",
            "ask",
            "order book",
            "orderbook",
            "book depth",
            "trades",
            "ohlcv",
            "settlement",
            "auction",
            "instrument list",
            "symbol list",
            "contract list",
        ),
        cap=6,
    )
    score += _term_score(
        text,
        (
            "exchange",
            "venue",
            "market surface",
            "asset class",
            "regional market",
            "local market",
            "derivatives",
            "futures",
            "options",
            "rates",
            "fx",
            "commodit",
            "credit",
            "lending",
            "prediction market",
            "event market",
            "sports odds",
        ),
        cap=4,
    )
    return score


def _scanner_expansion_score(text: str) -> int:
    return _term_score(
        text,
        (
            "depth enrichment",
            "candidate cap",
            "candidate generation",
            "scanner breadth",
            "scanner expansion",
            "starved venue",
            "exploration quota",
            "quality coverage",
            "quote normalization",
            "fx normalization",
            "regional normalization",
            "market-tested",
            "markets tested",
        ),
        cap=8,
    )


def _paper_scoring_score(text: str) -> int:
    return _term_score(
        text,
        (
            "score",
            "scoring",
            "paper scorer",
            "gate",
            "filter",
            "decay",
            "decayed confidence",
            "confidence decay",
            "freshness",
            "freshness horizon",
            "freshness_horizon_seconds",
            "guard",
            "signal age",
            "stale signal",
            "stale_signal_decayed",
            "sample-size",
            "sample size",
            "confirmation",
            "min emit confidence",
            "quarantine",
            "penalty",
            "eligibility",
            "admission",
            "ranking",
            "allocation",
        ),
        cap=8,
    )


def _route_intelligence_score(text: str) -> int:
    return _term_score(
        text,
        (
            "route",
            "borrow",
            "margin",
            "fee",
            "conditional",
            "capability",
            "permission",
            "broker",
            "api support",
            "jurisdiction",
            "account",
            "eligibility",
        ),
        cap=8,
    )


def _runtime_pipeline_integration_score(text: str) -> int:
    return _term_score(
        text,
        (
            "base_asset",
            "quote_asset",
            "asset context",
            "candidate builder",
            "paper trade",
            "trade recorder",
            "position serializer",
            "open position",
            "close record",
            "closed trade",
            "persist",
            "persistence",
            "preserve",
            "serialize",
            "serialization",
        ),
        cap=10,
    )


def _looks_like_runtime_implementation(payload: dict) -> bool:
    action = str(payload.get("action") or "")
    if action == "propose_code_change" or isinstance(payload.get("code_change"), dict):
        return True
    if action not in {
        "propose_build_task",
        "request_market_adapter",
        "request_data_source",
        "request_red_team",
        "propose_diagnostic_hypothesis",
    }:
        return False
    text = _payload_text(payload)
    implementation_terms = (
        "add ",
        "implement",
        "wire",
        "build",
        "create",
        "extend",
        "modify",
        "patch",
        "fix ",
        "repair",
        "upgrade",
        "integrat",
        "enforce",
        "validate",
        "fallback",
        "gate",
        "score",
        "normaliz",
        "ingest",
        "collect",
        "map ",
        "surface",
        "expose",
    )
    runtime_terms = (
        "runtime",
        "pipeline",
        "scanner",
        "adapter",
        "parser",
        "market data",
        "public data",
        "endpoint",
        "exchange",
        "feed",
        "ticker",
        "order book",
        "auction",
        "settlement",
        "instrument",
        "symbol",
        "ingestion",
        "recommendation",
        "json",
        "schema",
        "llm packet",
        "state packet",
        "report",
        "scoring",
        "quality",
        "depth",
        "candidate",
        "frontier",
        "fx",
        "quote",
        "route",
        "borrow",
        "paper",
        "module",
        "function",
        "tests",
    )
    if not any(term in text for term in implementation_terms):
        return False
    if not any(term in text for term in runtime_terms):
        return False
    manual_only_terms = (
        "human decision",
        "manual action",
        "open account",
        "account setup",
        "credentials",
        "jurisdiction decision",
        "broker approval",
    )
    if any(term in text for term in manual_only_terms) and not any(
        term in text for term in ("report", "packet", "scoring", "validation", "adapter", "parser", "scanner")
    ):
        return False
    return True


def classify_recommendation(payload: dict) -> str:
    action = str(payload.get("action") or "")
    if action == "propose_code_change":
        return "code_change"
    if action == "propose_strategy_lab_experiment":
        return "strategy_lab_experiment"
    if action == "propose_signal_variant":
        return "signal_variant"
    if _looks_like_runtime_implementation(payload):
        return "code_change"
    if action == "propose_diagnostic_hypothesis":
        return "diagnostic_hypothesis"
    text = _payload_text(payload)
    if any(
        term in text
        for term in (
            "failure filter",
            "stricter",
            "entry filter",
            "demote",
            "block",
            "losing",
            "poor performing",
            "underperforming",
            "negative performance",
            "low win rate",
            "signal filtering",
        )
    ):
        return "failure_filter"
    if any(
        term in text
        for term in (
            "route",
            "borrow",
            "margin",
            "broker",
            "permission",
            "fee",
            "api support",
            "account",
            "jurisdiction",
            "eligibility",
        )
    ):
        return "route_resolver"
    if any(term in text for term in ("adapter", "venue", "frontier", "underserved", "data source", "watchlist")):
        return "market_adapter"
    return "research_note"


def _infer_code_category(payload: dict) -> str:
    text = _payload_text(payload)
    if _contains_any(text, ("json", "schema", "swarm")):
        return "evolution_loop_improvement"
    adapter_score = _market_data_adapter_score(payload, text)
    scanner_score = _scanner_expansion_score(text)
    scoring_score = _paper_scoring_score(text)
    pipeline_score = _runtime_pipeline_integration_score(text)
    pipeline_asset_context = _contains_any(text, ("base_asset", "quote_asset", "asset context"))
    route_score = _route_intelligence_score(text)
    explicit_scoring = _contains_any(
        text,
        (
            "score",
            "scoring",
            "paper scorer",
            "gate",
            "gating",
            "guard",
            "ranking",
            "allocation",
            "eligibility",
            "admission",
            "quarantine",
            "freshness horizon",
            "stale signal",
            "stale_signal_decayed",
            "decayed confidence",
            "decay",
        ),
    )
    pipeline_handoff = _contains_any(
        text,
        (
            "persist",
            "persistence",
            "preserve",
            "serialize",
            "serialization",
            "candidate builder",
            "paper trade",
        ),
    )
    if pipeline_score >= 3 and pipeline_asset_context and pipeline_handoff:
        return "runtime_pipeline_integration"
    if explicit_scoring and adapter_score < 6:
        return "paper_scoring_logic"
    if scoring_score >= 2 and scoring_score >= adapter_score and scoring_score >= scanner_score:
        return "paper_scoring_logic"
    if scanner_score >= 2 and scanner_score > adapter_score:
        return "scanner_expansion"
    if adapter_score >= 4:
        return "public_data_adapter"
    if route_score >= 2:
        return "read_only_route_intelligence"
    if _contains_any(text, ("packet", "report", "dashboard")):
        return "llm_prompt_state_packet"
    if "recommendation" in text:
        return "evolution_loop_improvement"
    return "runtime_pipeline_integration"


def _normalize_expected_file_path(path: object) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"^(?:[-*•]\s+|\d+\.\s+)", "", normalized).strip()
    normalized = normalized.strip(" \t\r\n,;")
    while len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"`", "'", '"'}:
        normalized = normalized[1:-1].strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.replace("\\", "/").strip()


def _explicit_expected_files(payload: dict, code_change: dict) -> list[str]:
    for value in (code_change.get("expected_files"), payload.get("expected_files"), payload.get("files_expected_to_change")):
        if isinstance(value, str) and value.strip():
            raw_items = [line for line in value.splitlines() if line.strip()]
            if not raw_items:
                raw_items = [value]
        elif isinstance(value, list):
            raw_items = [str(item) for item in value if str(item).strip()]
        else:
            continue
        normalized_items: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            normalized = _normalize_expected_file_path(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_items.append(normalized)
        if normalized_items:
            return normalized_items
        if isinstance(value, list):
            return []
    return []


def _allowed_expected_file(path: str) -> bool:
    normalized = _normalize_expected_file_path(path)
    if not normalized:
        return False
    if normalized in RECOMMENDATION_ALLOWED_FILES:
        return True
    if normalized.startswith(("/", "./", "../")) or ":" in normalized:
        return False
    if "/../" in f"/{normalized}/" or normalized.endswith("/.."):
        return False
    return any(normalized.startswith(prefix) for prefix in RECOMMENDATION_ALLOWED_PATH_PREFIXES)


def _source_agent_for_recommendation(rec: dict, payload: dict) -> str:
    return str(
        payload.get("source_agent")
        or payload.get("agent_name")
        or rec.get("source_agent")
        or rec.get("agent_name")
        or payload.get("market_key")
        or "unknown"
    )


def _consumer_validation_audit(
    rec: dict,
    payload: dict,
    *,
    invalid_expected_files: list[str],
    invalid_implementation_mode: str | None,
) -> dict:
    fingerprint = _proposal_fingerprint(payload)
    reasons: list[str] = []
    if invalid_expected_files:
        reasons.append("disallowed_expected_files")
    if invalid_implementation_mode:
        reasons.append("invalid_implementation_mode")
    now = _utc_now_dt()
    suppressed_until = None
    if reasons:
        cached_until = _CONSUMER_REJECTION_CACHE.get(fingerprint)
        if cached_until and cached_until > now:
            suppressed_until = cached_until.isoformat()
        else:
            cached_until = now + dt.timedelta(seconds=RECOMMENDATION_REJECTION_TTL_SECONDS)
            _CONSUMER_REJECTION_CACHE[fingerprint] = cached_until
            suppressed_until = cached_until.isoformat()
    return {
        "validation_stage": "consumer",
        "source_agent": _source_agent_for_recommendation(rec, payload),
        "proposal_fingerprint": fingerprint,
        "rejection_reason": ", ".join(reasons) if reasons else None,
        "invalid_expected_files": invalid_expected_files,
        "invalid_implementation_mode": invalid_implementation_mode,
        "suppressed_until": suppressed_until,
    }


def _normalize_code_change_recommendation(rec: dict) -> dict:
    payload = dict(rec.get("payload") or {})
    code_change = dict(payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {})
    category = code_change.get("change_category") or payload.get("change_category") or _infer_code_category(payload)
    raw_expected_files = _explicit_expected_files(payload, code_change)
    expected_files: list[str] = []
    invalid_expected_files: list[str] = []
    for path in raw_expected_files:
        normalized = str(path).strip().replace("\\", "/")
        if not normalized:
            continue
        if _allowed_expected_file(normalized):
            expected_files.append(normalized)
        else:
            invalid_expected_files.append(normalized)
    inferred_implementation_mode = (
        "paper_policy" if str(category) == "paper_scoring_logic" else "runtime_active"
    )
    raw_implementation_mode = (
        code_change.get("implementation_mode")
        or payload.get("implementation_mode")
        or inferred_implementation_mode
    )
    implementation_mode = (
        raw_implementation_mode if raw_implementation_mode in {"paper_policy", "runtime_active"} else inferred_implementation_mode
    )
    tests_to_run = code_change.get("tests_to_run") or payload.get("tests_to_run") or []
    if isinstance(tests_to_run, str):
        tests_to_run = [tests_to_run] if tests_to_run.strip() else []
    else:
        tests_to_run = [str(item) for item in tests_to_run if str(item).strip()]
    consumer_validation = _consumer_validation_audit(
        rec,
        payload,
        invalid_expected_files=invalid_expected_files,
        invalid_implementation_mode=raw_implementation_mode if raw_implementation_mode != implementation_mode else None,
    )
    code_change.update(
        {
            "change_category": category,
            "implementation_mode": implementation_mode,
            "tests_to_run": tests_to_run,
            "rollback_criteria": code_change.get("rollback_criteria")
            or payload.get("rollback_criteria")
            or "Revert if tests fail, reports stop refreshing, or paper-only safety checks fail.",
            "evidence": code_change.get("evidence")
            or (payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}),
            "paper_testable_surface": code_change.get("paper_testable_surface")
            or payload.get("paper_testable_surface"),
            "behavioral_gate": code_change.get("behavioral_gate")
            or payload.get("behavioral_gate"),
            "target_selection_mode": "explicit" if expected_files else "repo_aware_preflight",
            "consumer_validation": consumer_validation,
        }
    )
    if expected_files:
        code_change["expected_files"] = expected_files
    else:
        code_change.pop("expected_files", None)
    payload["original_action"] = payload.get("action")
    payload["action"] = "propose_code_change"
    payload["change_category"] = category
    payload["implementation_mode"] = implementation_mode
    if expected_files:
        payload["expected_files"] = expected_files
    else:
        payload.pop("expected_files", None)
    payload["code_change"] = code_change
    payload["consumer_validation"] = consumer_validation
    payload["source_agent"] = payload.get("source_agent") or consumer_validation["source_agent"]
    if consumer_validation["proposal_fingerprint"]:
        payload["proposal_fingerprint"] = consumer_validation["proposal_fingerprint"]
    if not payload.get("proposed_change"):
        payload["proposed_change"] = payload.get("rationale") or rec.get("rationale") or rec.get("title")
    return {**rec, "payload": payload}


def _implemented_manual_category_exists(conn: sqlite3.Connection, category: str) -> bool:
    if category not in IMPLEMENTED_MANUAL_STATUSES:
        return False
    status, tables = IMPLEMENTED_MANUAL_STATUSES[category]
    for table in tables:
        row = conn.execute(f"select 1 from {table} where status = ? limit 1", (status,)).fetchone()
        if row:
            return True
    return False


def _text_for_payload(payload: dict) -> str:
    return " ".join(
        str(payload.get(key, ""))
        for key in ("title", "rationale", "proposed_change", "action", "market_key", "signal_key")
    ).lower()


def _duplicate_route_requirements_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return (
        "route requirement" in text
        or "execution route requirements" in text
        or ("conditional opportunit" in text and "requirements" in text)
    )


def _duplicate_frontier_adapter_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "data adapter",
            "market coverage",
            "venue adapter",
            "undercovered",
            "poor performance",
        )
    )


def _duplicate_frontier_data_quality_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "enhanced data coverage",
            "data quality",
            "order book",
            "orderbook",
            "market depth",
            "freshness",
            "slippage",
            "liquidity quality",
        )
    )


def _duplicate_signal_redesign_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return any(
        term in text
        for term in (
            "signal redesign",
            "root cause analysis",
            "root-cause analysis",
            "investigate and improve poorly performing signals",
            "improve frontier crypto spot signals across venues",
        )
    )


def _duplicate_frontier_systemic_redesign_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier" in text and any(
        term in text
        for term in (
            "systemic",
            "negative performance",
            "poor signal performance",
            "venue-map",
            "venue map",
            "underserved frontier",
        )
    )


def _duplicate_okx_reliable_outcomes_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "okx" in text and any(
        term in text
        for term in (
            "reliable outcome",
            "reliable label",
            "legacy_unverified",
            "variant learning",
            "valid labels",
        )
    )


def _duplicate_okx_basis_signal_research_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "okx" in text and "perp" in text and "funding" in text and any(
        term in text
        for term in (
            "basis signal",
            "basis signals",
            "funding basis signal",
            "funding basis signals",
            "investigate and improve okx",
        )
    )


def _duplicate_regional_frontier_data_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return "frontier crypto" in text and any(
        term in text
        for term in (
            "africa",
            "southeast asia",
            "emerging frontier",
            "regional frontier",
            "regional venue",
        )
    )


def _duplicate_regional_fx_frontier_prediction_pack_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    regional_fx = any(
        term in text
        for term in (
            "regional fx",
            "fx reference",
            "public fx midpoint",
            "fiat-stablecoin reference",
            "quote normalization",
            "africa rail",
            "african stablecoin",
        )
    )
    adaptive_depth = "frontier" in text and any(
        term in text
        for term in (
            "adaptive depth",
            "depth enrichment",
            "known quality",
            "quality coverage",
        )
    )
    prediction_intelligence = "prediction" in text and any(
        term in text
        for term in (
            "event classification",
            "event intelligence",
            "expired",
            "resolution",
            "order-book",
            "orderbook",
        )
    )
    return regional_fx or adaptive_depth or prediction_intelligence


def _duplicate_global_market_discovery_scan_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    if any(term in text for term in ("new unlisted market", "new venue not in scanner", "add unseen")):
        return False
    if "global_discovery|" in text or any(term in text for term in GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS):
        return True
    mentions_global_discovery = any(
        term in text for term in ("global market discovery", "global_market_discovery", "global discovery")
    )
    mentions_implemented_scan = any(
        term in text for term in ("scanner", "scan", "proxy", "seed", "surface list", "coverage map")
    )
    return mentions_global_discovery and mentions_implemented_scan


def _duplicate_strategy_reliability_pack_payload(payload: dict) -> bool:
    text = _text_for_payload(payload)
    return any(
        term in text
        for term in (
            "strategy reliability",
            "venue-direction reliability",
            "venue direction reliability",
            "frontier venue signal repair",
            "frontier long weak",
            "frontier short weak",
            "long-frontier",
            "short-frontier",
            "yahoo proxy short",
            "proxy short",
            "funding/basis split",
            "funding basis split",
            "positive slice expansion",
            "market-specific factors",
            "microstructure and liquidity",
            "microstructure divergence",
            "weak win-rate",
            "weak win rate",
            "expand okx funding",
            "expand gate frontier short",
            "expand mexc frontier short",
            "expand binance_us frontier short",
        )
    )


def _duplicate_self_improvement_open_pack_payload(payload: dict) -> bool:
    return is_duplicate_open_pack_text(_text_for_payload(payload))


def _stats_by_signal(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        """
    ).fetchall()
    return {row["signal_key"]: dict(row) for row in rows}


def _closed_metrics_since(conn: sqlite3.Connection, signal_key: str, since: str | None = None) -> dict:
    params: list[object] = [signal_key]
    clause = "signal_key = ? and status = 'closed' and pnl_bps is not null"
    if since:
        clause += " and closed_at >= ?"
        params.append(since)
    rows = conn.execute(
        f"""
        select pnl_bps
        from paper_trades
        where {clause}
        """,
        params,
    ).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows]
    if not pnls:
        return {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "best_bps": None, "worst_bps": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def _overall_metrics(conn: sqlite3.Connection, since: str | None = None) -> dict:
    params: list[object] = []
    clause = "status = 'closed' and pnl_bps is not null"
    if since:
        clause += " and closed_at >= ?"
        params.append(since)
    rows = conn.execute(
        f"""
        select pnl_bps
        from paper_trades
        where {clause}
        """,
        params,
    ).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows]
    if not pnls:
        return {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "best_bps": None, "worst_bps": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def _first_activation(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        select min(activated_at) as first_activation
        from self_improvement_experiments
        where activated_at is not null
        """
    ).fetchone()
    return row["first_activation"] if row else None


def _policy_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select status, count(*) as count
        from signal_policies
        group by status
        order by status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def _experiment_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select status, count(*) as count
        from self_improvement_experiments
        group by status
        order by status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def _policy_impact_summary(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    risk = (settings or {}).get("risk", {})
    default_notional = float(risk.get("paper_notional_usd", 1000.0))
    rows = conn.execute(
        """
        select status, allocation_multiplier, applied_count, filtered_count, opened_count
        from signal_policies
        """
    ).fetchall()
    applied = sum(int(row["applied_count"] or 0) for row in rows)
    filtered = sum(int(row["filtered_count"] or 0) for row in rows)
    opened = sum(int(row["opened_count"] or 0) for row in rows)
    blocked_notional = filtered * default_notional
    reduced_notional = 0.0
    for row in rows:
        multiplier = float(row["allocation_multiplier"] if row["allocation_multiplier"] is not None else 1.0)
        reduced_notional += int(row["opened_count"] or 0) * default_notional * max(0.0, 1.0 - multiplier)
    return {
        "policies_total": len(rows),
        "active_policy_count": len(active_signal_policies(conn)),
        "applied_checks": applied,
        "paper_entries_filtered": filtered,
        "paper_entries_opened_under_policy": opened,
        "default_paper_notional_usd": round(default_notional, 2),
        "estimated_notional_blocked_usd": round(blocked_notional, 2),
        "estimated_notional_reduced_usd": round(reduced_notional, 2),
        "estimated_total_risk_reduction_usd": round(blocked_notional + reduced_notional, 2),
    }


def _augment_experiment_progress(conn: sqlite3.Connection, item: dict) -> dict:
    output = dict(item)
    if output.get("task_type") != "failure_filter" or not output.get("signal_key"):
        return output
    activated_at = output.get("activated_at")
    baseline = output.get("baseline", {})
    post_activation = _closed_metrics_since(conn, output["signal_key"], activated_at)
    output["post_activation"] = post_activation
    if baseline.get("avg_pnl_bps") is not None and post_activation.get("avg_pnl_bps") is not None:
        output["delta_avg_pnl_bps"] = round(float(post_activation["avg_pnl_bps"]) - float(baseline["avg_pnl_bps"]), 3)
    return output


def _progress_summary(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    first_activation = _first_activation(conn)
    all_time = _overall_metrics(conn)
    since_activation = _overall_metrics(conn, first_activation) if first_activation else None
    delta = None
    if since_activation and all_time.get("avg_pnl_bps") is not None and since_activation.get("avg_pnl_bps") is not None:
        delta = round(float(since_activation["avg_pnl_bps"]) - float(all_time["avg_pnl_bps"]), 3)
    return {
        "first_activation": first_activation,
        "all_time": all_time,
        "since_first_activation": since_activation,
        "since_vs_all_time_avg_pnl_delta_bps": delta,
        "policy_status_counts": _policy_status_counts(conn),
        "experiment_status_counts": _experiment_status_counts(conn),
        "policy_impact": _policy_impact_summary(conn, settings),
        "timeline_path": str(TIMELINE_JSONL),
    }


def _append_timeline_snapshot(report: dict) -> None:
    summary = report.get("progress_summary", {})
    snapshot = {
        "generated_at": report.get("generated_at"),
        "active_policies": len(report.get("active_policies", [])),
        "consumed": len(report.get("consumed", [])),
        "evaluated": len(report.get("evaluated", [])),
        "expired": len(report.get("expired", [])),
        "policy_impact": summary.get("policy_impact", {}),
        "all_time": summary.get("all_time", {}),
        "since_first_activation": summary.get("since_first_activation"),
        "experiment_status_counts": summary.get("experiment_status_counts", {}),
    }
    with TIMELINE_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, sort_keys=True) + "\n")


def _target_signals(conn: sqlite3.Connection, payload: dict) -> list[str]:
    stats = _stats_by_signal(conn)
    targets: list[str] = []
    if payload.get("signal_key"):
        targets.append(str(payload["signal_key"]))
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    for item in evidence.get("signals", []):
        if isinstance(item, dict) and item.get("signal_key"):
            targets.append(str(item["signal_key"]))
    market_key = payload.get("market_key")
    if market_key:
        prefix = str(market_key)
        for key in stats:
            if key.startswith(prefix + "|"):
                targets.append(key)
    deduped = []
    seen = set()
    for key in targets:
        if key in seen or key not in stats:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _policy_for_signal(stats: dict, settings: dict) -> dict:
    risk = settings.get("risk", {})
    improvement_cfg = settings.get("self_improvement", {})
    safety_cfg = settings.get("signal_safety", {})
    avg = float(stats.get("avg_pnl_bps") or 0)
    win_rate = float(stats.get("win_rate") or 0)
    closed = int(stats.get("closed_count") or 0)
    severe_loss = avg <= -75 and win_rate < 0.45 and closed >= 5
    moderate_loss = avg <= -20 or win_rate < 0.4
    min_edge = max(float(risk.get("min_net_edge_bps", 2.0)) + (6.0 if severe_loss else 3.0), 5.0)
    max_spread = min(float(risk.get("max_spread_bps", 8.0)), 4.0 if severe_loss else 5.0)
    exploration = exploration_enabled(settings)
    return {
        "min_score_delta": 12.0 if severe_loss else 7.0 if moderate_loss else 4.0,
        "min_net_edge_bps": round(min_edge, 3),
        "max_spread_bps": round(max_spread, 3),
        "allocation_multiplier": 0.25 if severe_loss and exploration else 0.0 if severe_loss else 0.25 if moderate_loss else 0.5,
        "pause_entries": severe_loss and not exploration,
        "would_pause_outside_exploration": severe_loss and exploration,
        "expires_after_trades": int(improvement_cfg.get("default_policy_trade_ttl", 30)),
        "allow_recovery_probes": True,
        "recovery_probe_every_n_reviews": int(
            improvement_cfg.get("recovery_probe_every_reviews", safety_cfg.get("recovery_probe_every_reviews", 25))
        ),
        "recovery_probe_allocation_multiplier": float(
            improvement_cfg.get(
                "recovery_probe_allocation_multiplier",
                safety_cfg.get("recovery_probe_allocation_multiplier", 0.1),
            )
        ),
        "release_criteria": {
            "min_closed_trades": int(safety_cfg.get("release_min_recovery_trades", 5)),
            "min_avg_pnl_bps": float(safety_cfg.get("release_min_avg_pnl_bps", 10.0)),
            "min_win_rate": float(safety_cfg.get("release_min_win_rate", 0.55)),
            "max_worst_bps": float(safety_cfg.get("release_max_worst_bps", -500.0)),
        },
        "reason": "severe_loss_pause" if severe_loss else "loss_tightening",
    }


def _policy_id(source_id: str, signal_key: str, policy: dict) -> str:
    raw = json.dumps({"source": source_id, "signal_key": signal_key, "policy": policy}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _active_policy_exists(conn: sqlite3.Connection, signal_key: str, policy_type: str) -> bool:
    row = conn.execute(
        """
        select 1
        from signal_policies
        where status in ('active', 'promoted')
          and signal_key = ?
          and policy_type = ?
          and (expires_after_trades is null or applied_count < expires_after_trades)
        limit 1
        """,
        (signal_key, policy_type),
    ).fetchone()
    return row is not None


def _active_governor_policy(conn: sqlite3.Connection, signal_key: str) -> dict | None:
    row = conn.execute(
        """
        select policy_id, policy_json, applied_count, filtered_count, opened_count
        from signal_policies
        where status in ('active', 'promoted')
          and signal_key = ?
          and policy_type = 'safety_governor'
        order by created_at desc
        limit 1
        """,
        (signal_key,),
    ).fetchone()
    if not row:
        return None
    try:
        policy = json.loads(row["policy_json"] or "{}")
    except json.JSONDecodeError:
        policy = {}
    return {
        "policy_id": row["policy_id"],
        "governor_mode": policy.get("governor_mode"),
        "applied_count": row["applied_count"],
        "filtered_count": row["filtered_count"],
        "opened_count": row["opened_count"],
    }


def consolidate_active_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select policy_id, experiment_id, signal_key, policy_type, pause_entries,
               min_score_delta, min_net_edge_bps, created_at, policy_json
        from signal_policies
        where status = 'active'
        order by signal_key, policy_type, pause_entries desc, min_score_delta desc,
                 min_net_edge_bps desc, created_at asc
        """
    ).fetchall()
    keep: set[tuple[str, str, str]] = set()
    superseded = []
    for row in rows:
        try:
            policy_payload = json.loads(row["policy_json"] or "{}")
        except json.JSONDecodeError:
            policy_payload = {}
        context_filter = policy_payload.get("context_filter") if row["policy_type"] == "contextual_failure_filter" else None
        context_key = json.dumps(context_filter or {}, sort_keys=True)
        key = (row["signal_key"], row["policy_type"], context_key)
        if key not in keep:
            keep.add(key)
            continue
        conn.execute("update signal_policies set status = 'superseded' where policy_id = ?", (row["policy_id"],))
        conn.execute(
            """
            update self_improvement_experiments
            set status = 'superseded',
                decision = 'superseded_by_policy_consolidation',
                completed_at = coalesce(completed_at, ?),
                reflection = 'Duplicate active policy for the same signal/type was consolidated.'
            where id = ? and status = 'active'
            """,
            (_utc_now(), row["experiment_id"]),
        )
        superseded.append({"policy_id": row["policy_id"], "experiment_id": row["experiment_id"], "signal_key": row["signal_key"]})
    conn.commit()
    return superseded


def _execute_failure_filter(conn: sqlite3.Connection, rec: dict, settings: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_strategy_reliability_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "strategy_reliability_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "strategy_reliability_pack_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_frontier_systemic_redesign_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_systemic_redesign"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_systemic_redesign_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_okx_reliable_outcomes_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_reliable_outcomes"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_reliable_outcomes_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    if _duplicate_signal_redesign_payload(payload) and _implemented_manual_category_exists(conn, "signal_redesign"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "signal_redesign_already_implemented",
                "signal_key": payload.get("signal_key"),
            }
        ]
    stats = _stats_by_signal(conn)
    created = []
    max_policies = int(settings.get("self_improvement", {}).get("max_policies_per_task", 5))
    for signal_key in _target_signals(conn, payload)[:max_policies]:
        if _active_policy_exists(conn, signal_key, "failure_filter"):
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "failure_filter_already_active",
                }
            )
            continue
        governor = _active_governor_policy(conn, signal_key)
        if governor:
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "covered_by_signal_safety_governor",
                    "governor_policy": governor,
                }
            )
            continue
        item = stats[signal_key]
        if int(item.get("closed_count") or 0) < int(settings.get("learning", {}).get("min_samples_for_adjustment", 3)):
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "not_enough_closed_trades",
                    "signal_stats": item,
                }
            )
            continue
        if float(item.get("avg_pnl_bps") or 0) >= 0 and float(item.get("score_adjustment") or 0) >= 0:
            created.append(
                {
                    "signal_key": signal_key,
                    "action_status": "skipped",
                    "skip_reason": "signal_not_losing_after_learning_adjustment",
                    "signal_stats": item,
                }
            )
            continue
        policy = _policy_for_signal(item, settings)
        baseline = _closed_metrics_since(conn, signal_key)
        source_agent = payload.get("agent_name")
        experiment_id = add_self_improvement_experiment(
            conn,
            rec["recommendation_id"],
            source_agent,
            "failure_filter",
            int(payload.get("priority", 80)),
            payload.get("market_key"),
            signal_key,
            f"LLM failure filter should improve paper outcomes for {signal_key}.",
            payload.get("proposed_change") or payload.get("rationale") or "Apply stricter paper-only failure filter.",
            baseline,
            policy,
        )
        if not experiment_id:
            continue
        pid = _policy_id(rec["recommendation_id"], signal_key, policy)
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        inserted = add_signal_policy(
            conn,
            pid,
            experiment_id,
            rec["recommendation_id"],
            signal_key,
            payload.get("market_key"),
            "failure_filter",
            policy,
            {"recommendation": payload, "signal_stats": item, "baseline": baseline, "evidence": evidence},
        )
        if inserted:
            add_memory_fact(
                conn,
                "self_improvement_policy",
                signal_key,
                "activated",
                policy["reason"],
                0.82,
                "self_improvement_executor",
                {"experiment_id": experiment_id, "policy_id": pid, "policy": policy, "baseline": baseline},
            )
            created.append(
                {
                    "experiment_id": experiment_id,
                    "policy_id": pid,
                    "signal_key": signal_key,
                    "policy": policy,
                    "action_status": "created",
                }
            )
    return created


def _execute_route_resolver(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_global_market_discovery_scan_payload(payload) and _implemented_manual_category_exists(
        conn, "global_market_discovery_scan"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "global_market_discovery_scan_already_implemented",
                "market_key": payload.get("market_key") or "global_discovery",
                "route_key": payload.get("signal_key") or "global_market_discovery",
            }
        ]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    if _duplicate_route_requirements_payload(payload) and _implemented_manual_category_exists(conn, "route_requirements"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "route_requirements_already_implemented",
                "market_key": payload.get("market_key") or "execution_routes",
                "route_key": payload.get("signal_key") or "conditional_opportunities",
            }
        ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    market_key = str(payload.get("market_key") or "execution_routes")
    route_key = str(payload.get("signal_key") or "conditional_opportunities")
    created = add_route_probe_task(
        conn,
        rec["recommendation_id"],
        market_key,
        route_key,
        int(payload.get("priority", 75)),
        "read_only_capability_probe",
        payload.get("proposed_change") or payload.get("rationale") or "Discover paper route capability gaps.",
        evidence,
    )
    if not created:
        return []
    experiment_id = add_self_improvement_experiment(
        conn,
        rec["recommendation_id"],
        payload.get("agent_name"),
        "route_resolver",
        int(payload.get("priority", 75)),
        market_key,
        route_key,
        "Route capability discovery should reduce conditional unknowns.",
        "Create read-only broker/borrow/margin/fee/API capability probe task.",
        {"conditional_count": evidence.get("conditional_count")},
        {"probe_type": "read_only_capability_probe"},
    )
    return [{"experiment_id": experiment_id, "market_key": market_key, "route_key": route_key}]


def _execute_adapter_spec(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_global_market_discovery_scan_payload(payload) and _implemented_manual_category_exists(
        conn, "global_market_discovery_scan"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "global_market_discovery_scan_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "global_market_discovery",
            }
        ]
    if _duplicate_regional_fx_frontier_prediction_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_fx_frontier_prediction_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_fx_frontier_prediction_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "regional_fx_frontier_prediction_pack",
            }
        ]
    if _duplicate_self_improvement_open_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "self_improvement_open_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "self_improvement_open_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "self_improvement_open_pack",
            }
        ]
    if _duplicate_strategy_reliability_pack_payload(payload) and _implemented_manual_category_exists(
        conn, "strategy_reliability_pack"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "strategy_reliability_pack_already_implemented",
                "market_key": payload.get("market_key") or payload.get("signal_key") or "strategy_reliability",
            }
        ]
    if _duplicate_frontier_systemic_redesign_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_systemic_redesign"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_systemic_redesign_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_okx_reliable_outcomes_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_reliable_outcomes"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_reliable_outcomes_already_implemented",
                "market_key": payload.get("market_key") or "OKX|perp_funding_basis",
            }
        ]
    if _duplicate_frontier_data_quality_payload(payload) and _implemented_manual_category_exists(
        conn, "frontier_data_quality"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_data_quality_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_signal_redesign_payload(payload) and _implemented_manual_category_exists(conn, "signal_redesign"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "signal_redesign_already_implemented",
                "market_key": payload.get("market_key") or "signal_redesign",
            }
        ]
    if _duplicate_okx_basis_signal_research_payload(payload) and _implemented_manual_category_exists(
        conn, "okx_basis_signal_research"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "okx_basis_signal_research_already_implemented",
                "market_key": payload.get("market_key") or "OKX|perp_funding_basis",
            }
        ]
    if _duplicate_regional_frontier_data_payload(payload) and _implemented_manual_category_exists(
        conn, "regional_frontier_data"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "regional_frontier_data_already_implemented",
                "market_key": payload.get("market_key") or "frontier_crypto_venue_map",
            }
        ]
    if _duplicate_frontier_adapter_payload(payload) and _implemented_manual_category_exists(conn, "frontier_crypto_adapter"):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "frontier_crypto_adapter_already_implemented",
                "market_key": payload.get("market_key") or "market_adapter",
            }
        ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    market_key = str(payload.get("market_key") or "market_adapter")
    title = str(payload.get("title") or "Market adapter spec")[:180]
    spec = {
        "goal": payload.get("proposed_change") or payload.get("rationale"),
        "source_agent": payload.get("agent_name"),
        "required_checks": [
            "public API/data availability",
            "latency/reachability",
            "market hours or funding cadence",
            "paper-trade feasibility",
            "route and jurisdiction caveats",
        ],
        "allowed_mode": "research_spec_only",
    }
    created = add_adapter_spec(
        conn,
        rec["recommendation_id"],
        market_key,
        int(payload.get("priority", 70)),
        title,
        spec,
        evidence,
    )
    if not created:
        return []
    experiment_id = add_self_improvement_experiment(
        conn,
        rec["recommendation_id"],
        payload.get("agent_name"),
        "market_adapter",
        int(payload.get("priority", 70)),
        market_key,
        None,
        "Adapter research should expand market coverage without live execution risk.",
        "Create adapter research spec and rank by data availability/latency.",
        {},
        spec,
    )
    return [{"experiment_id": experiment_id, "market_key": market_key, "title": title}]


def _execute_signal_variant(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    config = payload.get("variant_config")
    if not isinstance(config, dict):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "missing_or_invalid_variant_config",
            }
        ]
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    try:
        created = create_proposed_variant(
            conn,
            title=str(payload.get("title") or "LLM frontier signal challenger"),
            config=config,
            source_recommendation_id=rec["recommendation_id"],
            source_agent=payload.get("agent_name"),
            source_model=model.get("name"),
            evidence={
                **evidence,
                "rationale": payload.get("rationale"),
                "proposed_change": payload.get("proposed_change"),
                "estimated_cost_usd": model.get("estimated_cost_usd"),
            },
        )
    except ValueError as exc:
        return [
            {
                "action_status": "skipped",
                "skip_reason": "variant_validation_failed",
                "validation_error": str(exc),
            }
        ]
    return [
        {
            **created,
            "action_status": "created" if created.get("created") else "skipped",
            "skip_reason": None if created.get("created") else "variant_already_exists",
        }
    ]


def _execute_diagnostic_hypothesis(conn: sqlite3.Connection, rec: dict) -> list[dict]:
    payload = rec["payload"]
    if _duplicate_global_market_discovery_scan_payload(payload) and _implemented_manual_category_exists(
        conn, "global_market_discovery_scan"
    ):
        return [
            {
                "action_status": "skipped",
                "skip_reason": "global_market_discovery_scan_already_implemented",
                "signal_key": payload.get("signal_key") or payload.get("market_key") or "global_market_discovery",
            }
        ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    signal = str(payload.get("signal_key") or payload.get("market_key") or "signal_redesign")
    add_growth_experiment(
        conn,
        int(payload.get("priority", 75)),
        signal,
        str(payload.get("title") or "LLM diagnostic hypothesis"),
        str(payload.get("proposed_change") or payload.get("rationale") or "Test causal signal hypothesis."),
        {
            **evidence,
            "agent_name": payload.get("agent_name"),
            "model": payload.get("model"),
            "diagnostic_only": True,
        },
    )
    return [{"action_status": "created", "signal_key": signal}]


def _execute_strategy_lab_experiment(conn: sqlite3.Connection, rec: dict, settings: dict) -> list[dict]:
    return [enqueue_strategy_owner_recommendation(conn, rec, settings)]


def evaluate_active_experiments(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("self_improvement", {})
    min_trades = int(cfg.get("min_eval_closed_trades", 10))
    min_improvement = float(cfg.get("promote_min_improvement_bps", 3.0))
    max_regression = float(cfg.get("revert_max_regression_bps", 8.0))
    evaluated = []
    rows = conn.execute(
        """
        select id, activated_at, task_type, signal_key, baseline_json, status
        from self_improvement_experiments
        where status = 'active' and task_type = 'failure_filter' and signal_key is not null
        """
    ).fetchall()
    for row in rows:
        activated_at = row["activated_at"]
        baseline = json.loads(row["baseline_json"] or "{}")
        current = _closed_metrics_since(conn, row["signal_key"], activated_at)
        evaluation = {"baseline": baseline, "post_activation": current, "checked_at": _utc_now()}
        if int(current["closed_count"] or 0) < min_trades:
            continue
        baseline_avg = baseline.get("avg_pnl_bps")
        current_avg = current.get("avg_pnl_bps")
        if baseline_avg is None or current_avg is None:
            continue
        delta = float(current_avg) - float(baseline_avg)
        evaluation["delta_avg_pnl_bps"] = round(delta, 3)
        baseline_wr = baseline.get("win_rate") or 0
        current_wr = current.get("win_rate") or 0
        if delta >= min_improvement and float(current_wr) >= float(baseline_wr) - 0.05:
            status = "promoted"
            decision = "promote"
            reflection = "Paper outcomes improved after the policy; keep the policy active as promoted."
        elif delta <= -max_regression:
            status = "reverted"
            decision = "revert"
            reflection = "Paper outcomes regressed after the policy; revert this policy."
        else:
            status = "active"
            decision = "needs_more_data"
            reflection = "Evaluation window is mixed; keep collecting evidence."
        update_experiment_evaluation(conn, int(row["id"]), status, decision, evaluation, reflection)
        evaluated.append({"experiment_id": int(row["id"]), "status": status, "decision": decision, "evaluation": evaluation})
    return evaluated


def _select_recommendations_for_lane(
    queued_recommendations: list[dict],
    max_tasks: int,
    *,
    include_code_changes: bool,
) -> list[tuple[dict, str]]:
    selected: list[tuple[dict, str]] = []
    for rec in queued_recommendations:
        task_type = classify_recommendation(rec["payload"])
        if task_type == "code_change" and not include_code_changes:
            continue
        selected.append((rec, task_type))
        if len(selected) >= max_tasks:
            break
    return selected


def run_auto_improvement(
    conn: sqlite3.Connection,
    settings: dict,
    *,
    include_code_changes: bool | None = None,
) -> dict:
    cfg = settings.get("self_improvement", {})
    if not cfg.get("enabled", True):
        return write_reports(conn, {"enabled": False}, settings=settings)
    if include_code_changes is None:
        include_code_changes = bool(cfg.get("process_code_changes_in_radar_loop", False))

    expired = expire_signal_policies(conn)
    evaluated = evaluate_active_experiments(conn, settings)
    code_evolution_evaluated = evaluate_code_evolution(
        conn,
        settings,
        resume_paused=bool(include_code_changes),
    )
    registry_backfill = backfill_open_artifacts(conn)
    deployed_reconciliation = reconcile_deployed_artifacts(conn)
    consumed = []
    max_tasks = int(cfg.get("max_tasks_per_loop", 5))
    scan_limit = max_tasks if include_code_changes else max(max_tasks * 100, max_tasks)
    queued_recommendations = llm_recommendations_for_auto_execution(
        conn,
        limit=scan_limit,
        include_code_changes=include_code_changes,
    )
    selected_recommendations = _select_recommendations_for_lane(
        queued_recommendations,
        max_tasks,
        include_code_changes=include_code_changes,
    )

    for rec, task_type in selected_recommendations:
        payload = rec["payload"]
        topic = claim_topic(
            conn,
            payload=payload,
            topic_type=task_type,
            priority=int(payload.get("priority", rec.get("priority", 50)) or 50),
            evidence=payload.get("evidence"),
            source_ref=f"llm_recommendations:{rec['recommendation_id']}",
        )
        if topic.duplicate and topic.canonical_row_id:
            update_llm_recommendation_status(conn, rec["recommendation_id"], "auto_deduplicated")
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "status": "auto_deduplicated",
                    "topic_key": topic.topic_key,
                    "canonical_artifact": f"{topic.canonical_table}:{topic.canonical_row_id}",
                }
            )
            continue
        created: list[dict] = []
        if task_type == "failure_filter":
            created = _execute_failure_filter(conn, rec, settings)
        elif task_type == "route_resolver":
            created = _execute_route_resolver(conn, rec)
        elif task_type == "market_adapter":
            created = _execute_adapter_spec(conn, rec)
        elif task_type == "signal_variant":
            created = _execute_signal_variant(conn, rec)
        elif task_type == "diagnostic_hypothesis":
            created = _execute_diagnostic_hypothesis(conn, rec)
        elif task_type == "strategy_lab_experiment":
            created = _execute_strategy_lab_experiment(conn, rec, settings)
        elif task_type == "code_change":
            created = process_code_change_recommendation(conn, _normalize_code_change_recommendation(rec), settings)

        if task_type == "strategy_lab_experiment" and created:
            artifact = created[0]
            artifact_id = artifact.get("task_id") or rec["recommendation_id"]
            bind_artifact(conn, topic.topic_key, "strategy_owner_tasks", artifact_id)
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "status": artifact.get("action_status"),
                    "created": created,
                }
            )
            # The Strategy Owner sets owner_queued or linked_existing_task. Do
            # not collapse that durable lifecycle into a premature executed flag.
            continue

        created_artifacts = [item for item in created if item.get("action_status", "created") == "created"]
        if created_artifacts:
            artifact = created_artifacts[0]
            artifact_table = artifact.get("artifact_table") or {
                "failure_filter": "self_improvement_experiments",
                "route_resolver": "route_probe_tasks",
                "market_adapter": "adapter_specs",
                "signal_variant": "signal_variants",
                "diagnostic_hypothesis": "growth_experiments",
                "strategy_lab_experiment": "strategy_owner_tasks",
                "code_change": "code_evolution_proposals",
            }.get(task_type)
            artifact_id = next(
                (
                    artifact.get(key)
                    for key in (
                        "id", "experiment_id", "task_id", "spec_id", "variant_id",
                        "strategy_lab_id", "proposal_id",
                    )
                    if artifact.get(key) is not None
                ),
                rec["recommendation_id"],
            )
            if artifact_table:
                bind_artifact(conn, topic.topic_key, artifact_table, artifact_id)
            update_llm_recommendation_status(conn, rec["recommendation_id"], "auto_executed")
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "created": created,
                }
            )
        else:
            update_llm_recommendation_status(conn, rec["recommendation_id"], "auto_skipped")
            consumed.append(
                {
                    "recommendation_id": rec["recommendation_id"],
                    "task_type": task_type,
                    "title": rec["title"],
                    "created": [],
                    "status": "auto_skipped",
                }
            )

    superseded = consolidate_active_policies(conn)
    report = {
        "enabled": True,
        "generated_at": _utc_now(),
        "code_changes_enabled": bool(include_code_changes),
        "consumed": consumed,
        "evaluated": evaluated,
        "code_evolution_evaluated": code_evolution_evaluated,
        "expired": expired,
        "superseded": superseded,
        "active_policies": active_signal_policies(conn),
        "experiments": open_self_improvement_experiments(conn, limit=50),
        "route_probe_tasks": open_route_probe_tasks(conn, limit=50),
        "adapter_specs": open_adapter_specs(conn, limit=50),
        "code_evolution": write_code_evolution_reports(conn, settings),
        "recommendation_registry": registry_summary(conn),
        "recommendation_registry_backfill": registry_backfill,
        "deployed_artifact_reconciliation": deployed_reconciliation,
    }
    return write_reports(conn, report, settings=settings)


def record_review_policy_effects(conn: sqlite3.Connection, review: dict) -> None:
    for item in review.get("applied_policies", []):
        record_policy_application(
            conn,
            item["policy_id"],
            filtered=item.get("filtered", False),
        )


def record_open_policy_effects(conn: sqlite3.Connection, review: dict) -> None:
    for item in review.get("applied_policies", []):
        if item.get("filtered", False):
            continue
        record_policy_open(conn, item["policy_id"])


def write_reports(conn: sqlite3.Connection, report: dict | None = None, settings: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    expired = expire_signal_policies(conn)
    if report is None:
        report = {
            "enabled": True,
            "generated_at": _utc_now(),
        }
    else:
        report = dict(report)
    report["generated_at"] = _utc_now()
    report["expired"] = [*report.get("expired", []), *expired]
    report["active_policies"] = active_signal_policies(conn)
    report["experiments"] = [
        _augment_experiment_progress(conn, item)
        for item in open_self_improvement_experiments(conn, limit=50)
    ]
    report["route_probe_tasks"] = open_route_probe_tasks(conn, limit=50)
    report["adapter_specs"] = open_adapter_specs(conn, limit=50)
    report["progress_summary"] = _progress_summary(conn, settings)
    report["code_evolution"] = report.get("code_evolution") or write_code_evolution_reports(conn, settings)
    report["strategy_lab"] = report.get("strategy_lab") or strategy_lab_summary(conn)
    report["strategy_implementation_owner"] = strategy_owner_summary(conn, limit=40)
    report["recommendation_registry"] = registry_summary(conn)
    ACTIVE_POLICIES_JSON.write_text(json.dumps(report.get("active_policies", []), indent=2), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
    _append_timeline_snapshot(report)
    return report


def _report_markdown(report: dict) -> str:
    def metric(value: object, suffix: str = "") -> str:
        if value is None:
            return "n/a"
        return f"{value}{suffix}"

    progress = report.get("progress_summary", {})
    impact = progress.get("policy_impact", {})
    all_time = progress.get("all_time", {})
    since_activation = progress.get("since_first_activation")
    lines = [
        "# Self-Improvement Report",
        "",
        "This report tracks autonomous paper-only changes created from LLM recommendations.",
        "",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Timeline: `{progress.get('timeline_path') or TIMELINE_JSONL}`",
        "",
        "## Progress Summary",
        "",
        f"- Active policies: `{impact.get('active_policy_count', 0)}` of `{impact.get('policies_total', 0)}` total policy artifacts",
        f"- Policy checks applied: `{impact.get('applied_checks', 0)}`",
        f"- Paper entries filtered: `{impact.get('paper_entries_filtered', 0)}`",
        f"- Paper entries opened under policy: `{impact.get('paper_entries_opened_under_policy', 0)}`",
        f"- Estimated paper notional blocked/reduced: `${impact.get('estimated_total_risk_reduction_usd', 0)}`",
        f"- Policy statuses: `{progress.get('policy_status_counts', {})}`",
        f"- Experiment statuses: `{progress.get('experiment_status_counts', {})}`",
        f"- All-time closed paper PnL: `{metric(all_time.get('avg_pnl_bps'), ' bps')}` avg over `{all_time.get('closed_count', 0)}` trades, win rate `{metric(all_time.get('win_rate'))}`",
    ]
    if since_activation:
        lines.append(
            f"- Since first auto-policy: `{metric(since_activation.get('avg_pnl_bps'), ' bps')}` avg over "
            f"`{since_activation.get('closed_count', 0)}` trades, win rate `{metric(since_activation.get('win_rate'))}`, "
            f"delta vs all-time `{metric(progress.get('since_vs_all_time_avg_pnl_delta_bps'), ' bps')}`"
        )
    expired = report.get("expired", [])
    evaluated = report.get("evaluated", [])
    if expired or evaluated:
        lines.append(
            f"- This loop: `{len(expired)}` policy artifact(s) expired, `{len(evaluated)}` experiment(s) evaluated"
        )
    code_evolution = report.get("code_evolution") or {}
    if code_evolution:
        evo_summary = code_evolution.get("summary", {})
        lines.append(f"- Code evolution status: `{evo_summary.get('status_counts', {})}`")
    registry = report.get("recommendation_registry") or {}
    if registry:
        lines.append(
            f"- Recommendation topics: `{registry.get('topics', 0)}` canonical; "
            f"duplicates suppressed `{registry.get('duplicates_suppressed', 0)}`; "
            f"reopened from new evidence `{registry.get('reopened', 0)}`"
        )
    reconciliation = report.get("deployed_artifact_reconciliation") or {}
    if reconciliation:
        lines.append(
            f"- Already-deployed artifacts closed this loop: `{reconciliation.get('closed_count', 0)}` "
            f"across `{reconciliation.get('closed_by_category', {})}`; "
            f"reconciled total `{reconciliation.get('reconciled_total_count', 0)}`"
        )
    exploration = report.get("paper_exploration") or {}
    if exploration:
        exploration_summary = exploration.get("summary") or {}
        lines.append(
            f"- Paper exploration: direct `{exploration_summary.get('direct_paper_trades', 0)}`, "
            f"synthetic `{exploration_summary.get('synthetic_paper_trades', 0)}`, "
            f"capacity deferred (24h) `{exploration_summary.get('capacity_deferrals_24h', 0)}`, "
            f"true invalid-data rejections (24h) `{exploration_summary.get('true_invalid_data_rejections_24h', 0)}`"
        )
    context_drag = report.get("paper_context_drag") or {}
    if context_drag:
        lines.append(
            f"- Paper context drag: `{context_drag.get('down_ranked_candidates', 0)}` candidate(s) down-ranked "
            f"across `{context_drag.get('context_count', 0)}` realized contexts; eligibility unchanged."
        )
    lines.extend(
        [
            "",
            "## Latest Executor Activity",
            "",
        ]
    )
    consumed = report.get("consumed", [])
    if not consumed:
        lines.append("No new LLM tasks consumed this loop.")
    for item in consumed[:20]:
        created_items = [row for row in item.get("created", []) if row.get("action_status", "created") == "created"]
        skipped_items = [row for row in item.get("created", []) if row.get("action_status") == "skipped"]
        suffix = f", {len(skipped_items)} skipped" if skipped_items else ""
        lines.append(
            f"- `{item.get('task_type')}` {item.get('title')} -> "
            f"{len(created_items)} artifact(s){suffix}"
        )
        for skipped in skipped_items[:5]:
            lines.append(
                f"  - Skipped `{skipped.get('signal_key')}`: {skipped.get('skip_reason')}"
            )
    superseded = report.get("superseded", [])
    if superseded:
        lines.append("")
        lines.append(f"Consolidated {len(superseded)} duplicate active policy artifact(s).")
    if expired:
        lines.append("")
        lines.append(f"Expired {len(expired)} policy artifact(s) that reached their review TTL.")
    if evaluated:
        lines.append("")
        for item in evaluated[:10]:
            lines.append(
                f"Evaluated experiment #{item.get('experiment_id')} -> {item.get('decision')} "
                f"({item.get('status')})"
            )

    lines.extend(["", "## Active Policies", ""])
    policies = report.get("active_policies", [])
    if not policies:
        lines.append("No active signal policies.")
    for policy in policies[:30]:
        mode = "pause" if policy.get("pause_entries") else "tighten"
        lines.append(
            f"- `{policy['signal_key']}` {mode} policy `{policy['policy_id']}` "
            f"spread<={policy.get('max_spread_bps')} edge>={policy.get('min_net_edge_bps')} "
            f"score_delta={policy.get('min_score_delta')} applied={policy.get('applied_count')} "
            f"filtered={policy.get('filtered_count')} opened={policy.get('opened_count')} "
            f"ttl={policy.get('expires_after_trades')}"
        )
        context_filter = (policy.get("policy") or {}).get("context_filter")
        if context_filter:
            lines.append(f"  - Context filter: {context_filter}")

    safety = report.get("signal_safety_governor") or {}
    if safety:
        safety_summary = safety.get("summary", {})
        lines.extend(["", "## Signal Safety Governor", ""])
        lines.append(
            f"- Active governor policies: `{safety_summary.get('active_count', 0)}` "
            f"(quarantine `{safety_summary.get('quarantine_count', 0)}`, "
            f"probation `{safety_summary.get('probation_count', 0)}`)"
        )
        lines.append(f"- Released this loop: `{safety_summary.get('released_this_loop', 0)}`")
        active_governor = safety.get("active_governor_policies", [])
        if not active_governor:
            lines.append("- No active governor policies.")
        for item in active_governor[:20]:
            policy = item.get("policy", {})
            lines.append(
                f"- `{item['signal_key']}` `{policy.get('governor_mode')}` "
                f"allocation={item.get('allocation_multiplier')} applied={item.get('applied_count')} "
                f"filtered={item.get('filtered_count')} opened={item.get('opened_count')}"
            )
            lines.append(f"  - Recovery evidence: {item.get('recovery')}")
            lines.append(f"  - Release criteria: {policy.get('release_criteria')}")

    redesign = report.get("signal_redesign") or {}
    if redesign:
        redesign_summary = redesign.get("summary", {})
        lines.extend(["", "## Signal Redesign", ""])
        lines.append(f"- Active variant: `{redesign_summary.get('active_variant')}`")
        lines.append(
            f"- Reliable 60m metrics: `{redesign_summary.get('reliable_60m_paper_metrics')}`"
        )
        lines.append(f"- Label quality: `{redesign_summary.get('label_status_counts')}`")
        lines.append(
            f"- Valid outcome delay P95: `{redesign_summary.get('valid_delay_p95_seconds')}` seconds"
        )
        for variant in redesign.get("variants", [])[:10]:
            lines.append(
                f"- `{variant.get('variant_id')}` status=`{variant.get('status')}` "
                f"passes=`{variant.get('consecutive_passes')}`"
            )

    okx_research = report.get("okx_signal_research") or {}
    if okx_research:
        okx_summary = okx_research.get("summary", {})
        lines.extend(["", "## OKX Signal Research", ""])
        lines.append(f"- Active variant: `{okx_summary.get('active_variant')}`")
        lines.append(
            f"- Reliable 60m metrics: `{okx_summary.get('reliable_60m_paper_metrics')}`"
        )
        lines.append(f"- Label quality: `{okx_summary.get('label_status_counts')}`")
        lines.append(
            f"- Valid outcome delay P95: `{okx_summary.get('valid_delay_p95_seconds')}` seconds"
        )
        if okx_summary.get("carry_economics"):
            lines.append(f"- Carry economics: `{okx_summary.get('carry_economics')}`")
        carry = okx_research.get("carry_economics") or {}
        if carry:
            lines.append(f"- Carry report: `{carry.get('report')}`")
            for item in carry.get("top_positive_carry", [])[:5]:
                lines.append(
                    f"- Carry `{item.get('inst_id')}` `{item.get('direction')}` "
                    f"net=`{item.get('net_carry_edge_bps')}`bps status=`{item.get('carry_alignment_status')}`"
                )
        for variant in okx_research.get("variants", [])[:10]:
            lines.append(
                f"- `{variant.get('variant_id')}` status=`{variant.get('status')}` "
                f"passes=`{variant.get('consecutive_passes')}`"
            )

    strategy_reliability = report.get("strategy_reliability") or {}
    if strategy_reliability:
        reliability_summary = strategy_reliability.get("summary", {})
        lines.extend(["", "## Strategy Reliability Pack", ""])
        lines.append(f"- Candidates reviewed: `{reliability_summary.get('candidate_count', 0)}`")
        lines.append(f"- Annotated candidates: `{reliability_summary.get('annotated_count', 0)}`")
        lines.append(f"- Shadow/blocked: `{reliability_summary.get('shadow_or_blocked_count', 0)}`")
        lines.append(f"- Protected working slices: `{reliability_summary.get('protected_working_slice_count', 0)}`")
        lines.append(f"- Actions: `{reliability_summary.get('by_action', {})}`")
        lines.append(f"- Profiles: `{reliability_summary.get('by_profile', {})}`")
        for item in strategy_reliability.get("top_adjustments", [])[:10]:
            lines.append(
                f"- `{item.get('signal_key')}` `{item.get('direction')}` "
                f"action=`{item.get('action')}` allocation=`{item.get('allocation_multiplier')}` "
                f"reasons={item.get('reasons')}"
            )

    yahoo_counterfactual = report.get("yahoo_counterfactual") or {}
    if yahoo_counterfactual:
        lines.extend(["", "## Yahoo Proxy Counterfactuals", ""])
        lines.append(f"- Decision: `{yahoo_counterfactual.get('decision')}`")
        lines.append(f"- Reliable labels: `{yahoo_counterfactual.get('reliable_label_count', 0)}`")
        lines.append(f"- Horizon metrics: `{yahoo_counterfactual.get('horizon_metrics', {})}`")
        lines.append(
            f"- Direction-flip 60m: "
            f"`{(yahoo_counterfactual.get('counterfactuals') or {}).get('direction_flip_60m', {})}`"
        )
        lines.append(f"- Shadow recommendations: `{yahoo_counterfactual.get('shadow_recommendations', [])}`")

    reliability_cards = report.get("cross_context_reliability") or {}
    if reliability_cards:
        lines.extend(["", "## Cross-Context Reliability", ""])
        lines.append(f"- Contrast cards: `{reliability_cards.get('card_count', 0)}`")
        lines.append(f"- Guidance: {reliability_cards.get('guidance')}")
        for item in reliability_cards.get("cards", [])[:6]:
            lines.append(
                f"- `{item.get('group_key')}` delta=`{item.get('delta_avg_pnl_bps')}`bps "
                f"confidence=`{item.get('confidence')}` action=`{item.get('recommended_action')}`"
            )

    lab = report.get("strategy_lab") or {}
    if lab:
        lines.extend(["", "## Strategy Lab", ""])
        lines.append(f"- Total experiments: `{lab.get('total_experiments', 0)}`")
        lines.append(f"- Status counts: `{lab.get('status_counts', {})}`")
        lines.append(f"- Experiment types: `{lab.get('by_experiment_type', {})}`")
        lines.append(f"- Generated last cycle: `{lab.get('generated_candidates_last_cycle', 0)}`")
        lines.append(f"- Report: `{lab.get('report')}`")
        market_items = lab.get("recent_market_strategies") or [
            item for item in lab.get("recent", []) if item.get("experiment_type", "market_strategy") == "market_strategy"
        ]
        non_market_items = lab.get("recent_non_market_experiments") or [
            item for item in lab.get("recent", []) if item.get("experiment_type", "market_strategy") != "market_strategy"
        ]
        if market_items:
            lines.append("- Recent market strategies:")
        for item in market_items[:8]:
            evaluation = item.get("evaluation") or {}
            metrics = ((evaluation.get("outcomes") or {}).get("metrics") or {})
            lines.append(
                f"  - `{item.get('strategy_lab_id')}` type=`{item.get('experiment_type', 'market_strategy')}` "
                f"status=`{item.get('status')}` "
                f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
                f"hypothesis={item.get('hypothesis')}"
            )
        if non_market_items:
            lines.append("- Recent risk/system experiments:")
        for item in non_market_items[:8]:
            evaluation = item.get("evaluation") or {}
            metrics = ((evaluation.get("outcomes") or {}).get("metrics") or {})
            lines.append(
                f"  - `{item.get('strategy_lab_id')}` type=`{item.get('experiment_type')}` "
                f"status=`{item.get('status')}` "
                f"labels=`{metrics.get('count', 0)}` avg=`{metrics.get('avg_pnl_bps')}` "
                f"hypothesis={item.get('hypothesis')}"
            )

    expansion = report.get("expansion_map") or {}
    if expansion:
        frontier = expansion.get("frontier_crypto") or {}
        regional_fx = expansion.get("regional_fx_reference") or {}
        prediction = expansion.get("prediction_markets") or {}
        route = expansion.get("route_intelligence") or {}
        public_adapters = expansion.get("public_market_adapters") or {}
        adapter_capabilities = expansion.get("adapter_capabilities") or {}
        lines.extend(["", "## Expansion Map", ""])
        lines.append(
            f"- Frontier observations: `{frontier.get('observation_count', 0)}` "
            f"venues=`{frontier.get('venue_count', 0)}` symbols=`{frontier.get('symbol_count', 0)}`"
        )
        lines.append(
            f"- Frontier depth: selected `{frontier.get('depth_selected_count', 0)}`, "
            f"enriched `{frontier.get('depth_enriched_count', 0)}`, "
            f"known quality rate `{frontier.get('known_quality_rate')}`"
        )
        lines.append(
            f"- Frontier regional observations: `{frontier.get('regional_observation_count', 0)}` "
            f"quote_norm=`{frontier.get('by_quote_normalization', {})}`"
        )
        if regional_fx:
            lines.append(
                f"- Regional FX references: `{regional_fx.get('reference_count', 0)}` "
                f"stale=`{regional_fx.get('stale_count', 0)}` "
                f"providers=`{regional_fx.get('provider_status', [])}`"
            )
        lines.append(
            f"- Prediction markets: candidates `{prediction.get('candidate_count', 0)}` "
            f"orderbooks=`{prediction.get('by_orderbook_status', {})}` "
            f"expired_filtered=`{prediction.get('expired_filtered_count', 0)}` "
            f"event_review_queue=`{len(prediction.get('prediction_event_review_queue', []))}`"
        )
        lines.append(
            f"- Route intelligence: blockers `{route.get('blocker_counts', {})}`, "
            f"potentially executable soon `{route.get('potentially_executable_soon_count', 0)}`"
        )
        if public_adapters:
            lines.append(
                f"- Public adapter plugins: `{public_adapters.get('adapter_count', 0)}` adapters, "
                f"`{public_adapters.get('observation_count', 0)}` observations, "
                f"source health=`{public_adapters.get('by_source_status', {})}`"
            )
        if adapter_capabilities:
            lines.append(
                f"- Adapter capability registry: `{adapter_capabilities.get('inventory_count', 0)}` runtime adapters, "
                f"spec reconciliation=`{adapter_capabilities.get('by_status', {})}`"
            )

    admission = report.get("market_admission") or expansion.get("market_admission") or {}
    if admission:
        lines.extend(["", "## Market Admission", ""])
        lines.append(f"- States: `{admission.get('state_count', 0)}`")
        lines.append(f"- By stage: `{admission.get('by_stage', {})}`")
        lines.append(f"- By health: `{admission.get('by_health', {})}`")
        lines.append(f"- Exact blockers: `{admission.get('by_blocker', {})}`")
        lines.append(
            f"- Requested symbols observed: `{admission.get('requested_symbols_observed', 0)}`/"
            f"`{admission.get('requested_symbol_count', 0)}`"
        )
    admission_bridge = report.get("market_admission_bridge") or expansion.get("market_admission_bridge") or {}
    if admission_bridge:
        lines.extend(["", "## Admission-To-Strategy Bridge", ""])
        lines.append(f"- Actions created: `{admission_bridge.get('actions_created', 0)}`")
        lines.append(f"- Duplicate actions suppressed: `{admission_bridge.get('duplicates_suppressed', 0)}`")
        lines.append(f"- Prior-stage topics resolved: `{admission_bridge.get('prior_stage_topics_resolved', 0)}`")
        lines.append(f"- User capability constraints honored: `{admission_bridge.get('user_constraints_suppressed', 0)}`")
        lines.append(f"- By action: `{admission_bridge.get('by_action', {})}`")
        lines.append(f"- Report: `{RUNS_DIR / 'market_admission_report.md'}`")

    research_path = RUNS_DIR / "research_worker_latest.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            research = {"status": "unreadable"}
        research_summary = research.get("summary", {})
        lines.extend(["", "## Global Market Discovery", ""])
        lines.append(f"- Status: `{research.get('status')}`")
        lines.append(
            f"- Candidates this run: `{research_summary.get('candidate_count', 0)}`, "
            f"new `{research_summary.get('new_candidate_count', 0)}`, "
            f"total known `{research_summary.get('total_known_candidate_count', 0)}`"
        )
        lines.append(f"- Surface types: `{research_summary.get('by_surface_type', {})}`")
        lines.append(f"- Regions: `{research_summary.get('by_region', {})}`")
        lines.append(f"- Artifact inserts: `{research_summary.get('inserted_artifact_counts', {})}`")
        lines.append(f"- Report: `{RUNS_DIR / 'research_worker_report.md'}`")

    allocation_path = RUNS_DIR / "hunter_allocation_report.json"
    if allocation_path.exists():
        try:
            allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            allocation = {"enabled": "unreadable"}
        lines.extend(["", "## Hunter Allocation", ""])
        lines.append(f"- Enabled: `{allocation.get('enabled')}`")
        lines.append(f"- Slot targets: `{allocation.get('slot_targets', {})}`")
        lines.append(f"- Selected by bucket: `{allocation.get('selected_by_bucket', {})}`")
        lines.append(f"- Global discovery: `{allocation.get('global_discovery', {})}`")
        lines.append(f"- Report: `{RUNS_DIR / 'hunter_allocation_report.md'}`")

    open_pack = report.get("self_improvement_open_pack") or {}
    if open_pack:
        borrow = open_pack.get("route_borrow_intelligence") or {}
        africa = open_pack.get("africa_rail_watchlist") or {}
        kalshi = open_pack.get("kalshi_public_coverage") or {}
        diagnostics = open_pack.get("signal_repair_diagnostics") or {}
        lines.extend(["", "## Self-Improvement Open Pack", ""])
        lines.append(f"- Report: `{RUNS_DIR / 'self_improvement_open_pack.md'}`")
        lines.append(
            f"- Route-borrow records: `{borrow.get('record_count', 0)}`, "
            f"shadow-only unconfirmed `{borrow.get('shadow_only_unconfirmed_count', 0)}`"
        )
        lines.append(
            f"- Africa rail watchlist: venues `{africa.get('venue_count', 0)}`, "
            f"instruments `{africa.get('instrument_count', 0)}`, availability `{africa.get('by_venue_availability', {})}`"
        )
        lines.append(
            f"- Kalshi public coverage: candidates `{kalshi.get('current_candidate_count', 0)}`, "
            f"route blockers `{kalshi.get('route_blockers', {})}`"
        )
        lines.append(
            f"- Weak-signal diagnostics: frontier `{len(diagnostics.get('frontier_weak_signal_diagnostics', []))}`, "
            f"Yahoo `{len(diagnostics.get('yahoo_proxy_diagnostics', []))}`, "
            f"OKX `{len(diagnostics.get('okx_basis_funding_diagnostics', []))}`"
        )

    code_evolution = report.get("code_evolution") or {}
    if code_evolution:
        evo_summary = code_evolution.get("summary", {})
        lines.extend(["", "## AI Code Evolution", ""])
        lines.append(f"- Report: `{evo_summary.get('report')}`")
        lines.append(f"- Ledger: `{evo_summary.get('ledger')}`")
        lines.append(f"- Status counts: `{evo_summary.get('status_counts', {})}`")
        for item in evo_summary.get("latest", [])[:10]:
            lines.append(
                f"- `{item.get('proposal_id')}` `{item.get('category')}` "
                f"status=`{item.get('status')}` files=`{item.get('changed_files')}`"
            )

    lines.extend(["", "## Experiments", ""])
    experiments = report.get("experiments", [])
    if not experiments:
        lines.append("No experiments yet.")
    for exp in experiments[:30]:
        lines.append(
            f"- P{exp['priority']} #{exp['id']} `{exp['task_type']}` `{exp.get('signal_key') or exp.get('market_key')}` "
            f"status={exp['status']} decision={exp.get('decision')}"
        )
        baseline = exp.get("baseline", {})
        if baseline:
            lines.append(f"  - Baseline: {baseline}")
        if exp.get("post_activation"):
            lines.append(f"  - Post activation: {exp['post_activation']}")
        if exp.get("delta_avg_pnl_bps") is not None:
            lines.append(f"  - Delta avg PnL: {exp['delta_avg_pnl_bps']} bps")
        if exp.get("evaluation"):
            lines.append(f"  - Evaluation: {exp['evaluation']}")

    lines.extend(["", "## Route Probe Tasks", ""])
    probes = report.get("route_probe_tasks", [])
    if not probes:
        lines.append("No open route probe tasks.")
    for probe in probes[:20]:
        lines.append(f"- P{probe['priority']} `{probe['route_key']}` {probe['probe_type']}: {probe['rationale']}")

    lines.extend(["", "## Adapter Specs", ""])
    specs = report.get("adapter_specs", [])
    if not specs:
        lines.append("No open adapter specs.")
    for spec in specs[:20]:
        lines.append(f"- P{spec['priority']} `{spec['market_key']}` {spec['title']}")
    return "\n".join(lines) + "\n"
