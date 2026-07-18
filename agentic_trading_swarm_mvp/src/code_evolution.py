"""Paper-only code evolution governor.

This module lets frontier LLM recommendations become code changes only through
deterministic checks: allowlisted categories, safe paths, forbidden-behavior
scans, sandbox patch application, and tests. It never enables live trading,
credentials, broker writes, startup changes, or destructive data actions. It can
install repo-declared Python dependencies after sandbox validation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from cost_router import complete, completion_preflight_status
from evolution.archive import write_candidate_archive
from evolution.builder_context import build_builder_context, render_builder_context
from evolution.canary import run_radar_canary, skip_canary
from evolution.evaluator import (
    benchmark_builder_change,
    classify_sandbox_failure,
    evaluate_candidate,
    run_builder_failure_benchmark,
)
from evolution.worktree import (
    cleanup_worktree,
    commit_candidate,
    create_candidate_worktree,
    promote_candidate,
    release_preflight,
)
from storage import (
    RUNS_DIR,
    add_code_evolution_proposal,
    code_evolution_by_status,
    code_evolution_recent,
    update_code_evolution_proposal,
)


STRICT_REQUIRED_RECOMMENDATION_FIELDS = (
    "action",
    "priority",
    "title",
    "rationale",
    "market_key",
    "evidence",
    "proposed_change",
)
STRICT_REQUIRED_RECOMMENDATION_FIELDS_SET = set(STRICT_REQUIRED_RECOMMENDATION_FIELDS)
STRICT_RECOMMENDATION_JSON_SEPARATORS = (",", ":")
MIN_PAPER_CONFIRM_SCORE_MIN = 0.68
SUPPRESSION_ACTIONS = {
    "suppress_trade_generation_for_this_case",
    "no_trade",
    "monitor_only",
    "wait_for_complete_valid_json_recommendation_with_market_context",
}

_FORBIDDEN_MARKDOWN_TOKENS = (
    "```",
    "\n```",
    "**",
    "__",
)

_STRICT_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_OPTIONAL_RECOMMENDATION_FIELDS = {
    "code_change",
    "variant_config",
}
_REQUIRED_TOP_LEVEL_KEYS = STRICT_REQUIRED_RECOMMENDATION_FIELDS + tuple(_OPTIONAL_RECOMMENDATION_FIELDS)
_ALLOWED_TOP_LEVEL_KEYS = set(STRICT_REQUIRED_RECOMMENDATION_FIELDS) | _OPTIONAL_RECOMMENDATION_FIELDS

_PAPER_ONLY_RECOMMENDATION_FALLBACK = {
    "action": "propose_code_change",
    "priority": 0,
    "title": "Paper-only recommendation fallback",
    "rationale": "The primary recommendation was not a valid single JSON object, so a safe fallback was emitted.",
    "market_key": "paper.execution_route_hunter.output_contract",
    "evidence": {
        "impact": "Prevents silent drops and retry loops in downstream parsers.",
        "parser": "structured_guard",
        "paper_only": True,
    },
    "proposed_change": {
        "paper_only": True,
        "summary": "Ensure exactly one valid JSON object is emitted for paper-only recommendation packets.",
        "expected_result": "Downstream consumers receive a parseable object with required keys on every run.",
    },
}

_PAPER_ONLY_VOLATILITY_GATE_DEFAULTS = {
    "paper_only": True,
    "true_range_multiple_cap": 1.8,
    "trend_confirmation_threshold": "moderately_stricter",
    "momentum_confirmation_threshold": "moderately_stricter",
}

_PAPER_ONLY_CROSS_MARKET_CONFIRMATION = {
    "equities": "firm",
    "credit_spreads": "tightening",
    "usd_or_rates": "stable_or_less_adverse",
}


def _extract_single_json_object(text: str) -> dict[str, Any] | None:
    """Parse a single JSON object from strict model output.

    Accepts only one JSON object with optional surrounding whitespace.
    Rejects markdown fences, arrays, and trailing commentary.
    """

    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or any(token in stripped for token in _FORBIDDEN_MARKDOWN_TOKENS):
        return None
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _has_meaningful_value(value: Any) -> bool:
    if value in (None, "", {}, [], ()):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _recommendation_schema_error(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return "recommendation must be a JSON object"

    missing = [field for field in STRICT_REQUIRED_RECOMMENDATION_FIELDS if field not in candidate]
    if missing:
        return f"missing required fields: {', '.join(missing)}"

    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        return "evidence must be a JSON object"
    if not evidence:
        return "evidence must contain a non-empty value"

    proposed_change = candidate.get("proposed_change")
    if not isinstance(proposed_change, dict):
        return "proposed_change must be a JSON object"

    if "variant_config" in candidate and not isinstance(candidate.get("variant_config"), dict):
        return "variant_config must be a JSON object"

    if "code_change" in candidate and not isinstance(candidate.get("code_change"), dict):
        return "code_change must be a JSON object"

    return None


def coerce_paper_only_recommendation(candidate: Any) -> dict[str, Any]:
    parsed_candidate = candidate
    if isinstance(candidate, str):
        parsed_candidate = _extract_single_json_object(candidate)

    schema_error: str | None = None
    if isinstance(parsed_candidate, dict):
        evidence = parsed_candidate.get("evidence")
        if not isinstance(evidence, dict):
            schema_error = "evidence must be a JSON object"
        else:
            schema_error = _recommendation_schema_error(parsed_candidate)
    else:
        schema_error = _recommendation_schema_error(parsed_candidate)

    if schema_error is None:
        return parsed_candidate

    fallback = dict(_PAPER_ONLY_RECOMMENDATION_FALLBACK)
    fallback["evidence"] = dict(fallback["evidence"])
    fallback["evidence"]["schema_error"] = schema_error
    fallback["evidence"]["original_type"] = type(candidate).__name__
    fallback["proposed_change"] = dict(fallback["proposed_change"])
    fallback["proposed_change"]["schema_error"] = schema_error
    return fallback


def _find_array_path(value: Any, path: str = "$") -> str | None:
    if isinstance(value, (list, tuple)):
        return path
    if isinstance(value, dict):
        for key, nested_value in value.items():
            child_path = f"{path}.{key}" if path != "$" else f"$.{key}"
            found = _find_array_path(nested_value, child_path)
            if found:
                return found
    return None


def _recommendation_schema_error(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recommendation must be a JSON object"
    missing = [field for field in STRICT_REQUIRED_RECOMMENDATION_FIELDS if field not in value]
    if missing:
        return f"missing required fields: {', '.join(missing)}"
    unexpected = [key for key in value if key not in _ALLOWED_TOP_LEVEL_KEYS]
    if unexpected:
        return f"unexpected top-level fields: {', '.join(sorted(unexpected))}"
    for required_key in STRICT_REQUIRED_RECOMMENDATION_FIELDS:
        if not _has_meaningful_value(value.get(required_key)):
            return f"{required_key} must contain a non-empty value"
    if not isinstance(value.get("evidence"), dict) or not value["evidence"]:
        return "evidence must be a JSON object"
    if not isinstance(value.get("proposed_change"), dict) or not value["proposed_change"]:
        return "proposed_change must be a JSON object"
    for optional_key in _OPTIONAL_RECOMMENDATION_FIELDS:
        if optional_key in value and value[optional_key] is None:
            return f"{optional_key} must be omitted or contain an object"
    array_path = _find_array_path(value)
    if array_path:
        return f"arrays are not allowed in recommendation payloads: {array_path} (use objects or strings only)"
    for required_key in ("action", "priority", "title", "rationale", "market_key"):
        if isinstance(value.get(required_key), str) and any(
            token in value[required_key] for token in _FORBIDDEN_MARKDOWN_TOKENS
        ):
            return f"markdown is not allowed in {required_key}"
    return None


def _paper_only_recommendation_fallback() -> dict[str, Any]:
    """Return a defensive paper-only fallback recommendation payload."""

    return json.loads(json.dumps(_PAPER_ONLY_RECOMMENDATION_FALLBACK))


def _finalize_recommendation_payload(text: str) -> dict[str, Any]:
    """Return one validated recommendation object or a paper-only fallback."""

    parsed = _extract_single_json_object(text)
    if parsed is None:
        return _paper_only_recommendation_fallback()
    if _recommendation_schema_error(parsed) is not None:
        return _paper_only_recommendation_fallback()
    return parsed


def _strict_recommendation_json_error(text: str) -> str | None:
    """Validate model output as one strict recommendation object."""

    if not isinstance(text, str):
        return "recommendation output must be a string"
    stripped = text.strip()
    if not stripped:
        return "recommendation output must not be empty"
    if any(token in stripped for token in _FORBIDDEN_MARKDOWN_TOKENS):
        return "markdown fences are not allowed"
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return "recommendation must be a single JSON object"
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return f"invalid JSON: {exc.msg}"
    return _recommendation_schema_error(payload)


def _strict_recommendation_fallback(
    validation_error: str,
    *,
    market_key: str = "paper.cross_market.output_contract",
) -> dict[str, Any]:
    """Build a deterministic paper-only fallback recommendation object."""

    return {
        "action": "wait_for_complete_valid_json_recommendation_with_market_context",
        "priority": 90,
        "title": "Hold until single-object JSON recommendation is available",
        "rationale": (
            "The recommendation output failed strict schema validation, so the "
            "paper-only runner should suppress trade generation and wait for a "
            "complete single JSON object."
        ),
        "market_key": market_key,
        "evidence": {
            "constraint": "Output must remain paper-only with no live-trading instruction",
            "validation_error": validation_error,
        },
        "proposed_change": {
            "format_rule": "No markdown, no commentary, no arrays, no extra top-level keys beyond the allowed optional fields",
            "goal": "Return exactly one schema-complete JSON object on every recommendation",
            "required_fields": "action, priority, title, rationale, market_key, evidence, proposed_change",
            "safety_rule": "Keep all recommendations scoped to paper trading only",
        },
        "variant_config": {
            "mode": "paper_only",
            "schema_strict": True,
        },
    }


def _paper_hold_recommendation(
    validation_error: str,
    *,
    market_key: str = "paper.cross_market.output_contract",
) -> dict[str, Any]:
    """Build a strict paper-only fallback recommendation object."""

    return {
        "action": "monitor_only",
        "priority": 0,
        "title": "Paper-only output contract hold",
        "rationale": validation_error,
        "market_key": market_key,
        "evidence": {"validation_error": validation_error},
        "proposed_change": {
            "paper_only": True,
            "schema_enforcement": "strict_single_object",
            "validation_rule": "Reject any response that is not a single JSON object or that omits a required field.",
        },
    }


def normalize_strict_recommendation_payload(
    value: Any,
    *,
    market_key: str = "paper.cross_market.output_contract",
) -> dict[str, Any]:
    """Return a validated recommendation or a paper-only hold object."""

    validation_error = _recommendation_schema_error(value)
    if validation_error is None and isinstance(value, dict):
        return value
    return _paper_hold_recommendation(validation_error or "invalid recommendation payload", market_key=market_key)


def _recommendation_or_paper_hold(value: Any, *, validation_error: str | None = None) -> dict[str, Any]:
    """Return a validated recommendation or a strict paper-only hold fallback."""

    if isinstance(value, str):
        value = _extract_single_json_object(value) or {}
    recommendation = value if isinstance(value, dict) else {}
    error_message = validation_error or _recommendation_schema_error(recommendation)
    if error_message is None:
        return recommendation
    return _schema_complete_fallback_recommendation(error_message)


def _schema_complete_fallback_recommendation(error_message: str | None = None) -> dict[str, Any]:
    recommendation = {
        "action": "hold",
        "priority": "medium",
        "title": "Schema fallback for invalid cross-market output",
        "rationale": "Use a neutral paper-only recommendation when research output is incomplete so the pipeline remains stable.",
        "market_key": "paper_only.cross_market",
        "evidence": {
            "issue": "Validation failure on generated recommendation payload.",
            "safety": "Neutral hold recommendation avoids accidental strategy drift in paper trading.",
        },
        "proposed_change": {
            "paper_only": True,
            "summary": "Return validated fallback object and log the validation error for offline review.",
        },
    }
    if error_message:
        recommendation["evidence"]["validation_error"] = error_message
    return recommendation


def _finalize_recommendation_payload(payload: Any, *, validation_error: str | None = None) -> dict[str, Any]:
    if isinstance(payload, dict):
        schema_error = _recommendation_schema_error(payload)
        if schema_error is None:
            return payload
        validation_error = validation_error or schema_error
    return _schema_complete_fallback_recommendation(validation_error)


def _coerce_confirm_score_min(value: Any) -> tuple[float | None, str]:
    if isinstance(value, bool):
        return None, "confirm_score_min must be a number between 0 and 1"
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        try:
            score = float(value.strip())
        except ValueError:
            return None, "confirm_score_min must be a number between 0 and 1"
    else:
        return None, "confirm_score_min must be a number between 0 and 1"
    if not 0.0 <= score <= 1.0:
        return None, "confirm_score_min must be a number between 0 and 1"
    return score, ""


def _strip_markdown_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _has_forbidden_markdown_tokens(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _FORBIDDEN_MARKDOWN_TOKENS)


def _extract_single_json_object(text: str) -> str | None:
    """Extract the first and only JSON object from a model response."""

    if not isinstance(text, str):
        return None
    cleaned = _strip_markdown_fences(text)
    match = _STRICT_JSON_OBJECT_RE.search(cleaned)
    if not match:
        return None
    candidate = match.group(0).strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    trailing = cleaned[match.end() :].strip()
    leading = cleaned[: match.start()].strip()
    if leading or trailing:
        return None
    return candidate


def _default_recommendation(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    recommendation = {
        "action": "monitor_only",
        "priority": 1,
        "title": "Fallback paper-only recommendation",
        "rationale": "Validation failed; returning a minimal safe recommendation.",
        "market_key": "paper_global_macro_radar",
        "evidence": {
            "status": "fallback",
            "confirmation_requirement": (
                "Require alignment between equities, credit spreads, and USD or rates "
                "before changing paper positioning."
            ),
            "paper_confirmation_profile": dict(_PAPER_ONLY_CROSS_MARKET_CONFIRMATION),
        },
        "proposed_change": {
            "summary": "No-op fallback recommendation for paper-only workflows.",
            "safety": "paper_only",
        },
        "variant_config": dict(_PAPER_ONLY_VOLATILITY_GATE_DEFAULTS),
    }
    if isinstance(overrides, dict):
        variant_config = overrides.get("variant_config")
        if isinstance(variant_config, dict):
            recommendation["variant_config"].update(variant_config)
        for key, value in overrides.items():
            if key == "variant_config" or value is None:
                continue
            if _find_array_path(value):
                continue
            recommendation[key] = value
    schema_error = _recommendation_schema_error(recommendation)
    if schema_error:
        raise ValueError(schema_error)
    return recommendation


def default_paper_recommendation(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a canonical schema-valid fallback recommendation."""

    candidate = _default_recommendation(overrides)
    valid, _reason = validate_strict_recommendation_schema(candidate)
    if not valid:
        candidate = _default_recommendation()
    return json.loads(serialize_strict_recommendation(candidate))


def _regen_hint_for_invalid_recommendation(reason: str) -> str:
    return (
        "Return exactly one valid JSON object with required keys "
        f"{', '.join(STRICT_REQUIRED_RECOMMENDATION_FIELDS)}. "
        "Do not include markdown fences, trailing text, arrays, or multiple objects. "
        f"Validation issue: {reason}"
    )


def _coerce_recommendation_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    if _has_forbidden_markdown_tokens(value):
        return None
    extracted = _extract_single_json_object(value)
    if extracted is None:
        return None
    try:
        parsed = json.loads(extracted)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_recommendation_response(
    value: Any,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a single safe JSON object for planner consumers."""

    fallback_packet = default_paper_recommendation(fallback)
    candidate = _coerce_recommendation_object(value)
    if candidate is None:
        return fallback_packet
    missing_required = [
        key for key in STRICT_REQUIRED_RECOMMENDATION_FIELDS if key not in candidate
    ]
    if missing_required:
        return fallback_packet
    if _find_array_path(candidate) is not None:
        return fallback_packet
    if not isinstance(candidate.get("evidence"), dict):
        return fallback_packet
    if not _has_meaningful_value(candidate.get("action")):
        return fallback_packet
    if not _has_meaningful_value(candidate.get("title")):
        return fallback_packet
    if not _has_meaningful_value(candidate.get("rationale")):
        return fallback_packet
    if not _has_meaningful_value(candidate.get("market_key")):
        return fallback_packet
    if not _has_meaningful_value(candidate.get("proposed_change")):
        return fallback_packet
    valid, _reason = validate_strict_recommendation_schema(candidate)
    if not valid:
        return fallback_packet
    return json.loads(serialize_strict_recommendation(candidate))


def validate_strict_recommendation_schema(packet: Any) -> tuple[bool, str]:
    if not isinstance(packet, dict):
        return False, "recommendation must be a JSON object"
    missing = [field for field in STRICT_REQUIRED_RECOMMENDATION_FIELDS if field not in packet]
    if missing:
        return False, f"missing required fields: {', '.join(missing)}"
    for field in STRICT_REQUIRED_RECOMMENDATION_FIELDS:
        if not _has_meaningful_value(packet.get(field)):
            return False, f"{field} must be present and non-empty"
    if packet.get("action") == "live_trade":
        return False, "live trading is not permitted"
    if packet.get("paper_only") is False:
        return False, "paper_only must be true or omitted"
    evidence = packet.get("evidence")
    if not isinstance(evidence, dict):
        return False, "evidence must be a JSON object"
    proposed_change = packet.get("proposed_change")
    if not isinstance(proposed_change, dict):
        return False, "proposed_change must be a JSON object"
    return True, ""


def serialize_strict_recommendation(packet: dict[str, Any]) -> str:
    """Return the canonical single-object JSON representation."""

    valid, reason = validate_strict_recommendation_schema(packet)
    if not valid:
        raise ValueError(f"strict recommendation validation failed: {reason}")
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=STRICT_RECOMMENDATION_JSON_SEPARATORS,
    )


def validate_strict_recommendation_schema(packet: dict[str, Any]) -> tuple[bool, str]:
    """Validate a paper-only market recommendation packet.

    This is intentionally strict so incomplete frontier outputs do not flow
    into the paper runner or reporting pipeline as if they were usable signals.
    """

    if not isinstance(packet, dict):
        return False, "packet must be a mapping"
    allowed_keys = set(_REQUIRED_TOP_LEVEL_KEYS)
    extra_keys = sorted(set(packet) - allowed_keys)
    if extra_keys:
        return False, f"unexpected top-level keys: {', '.join(extra_keys)}"

    try:
        serialized = json.dumps(packet, ensure_ascii=False)
        reparsed = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        return False, f"packet must be JSON serializable: {exc}"
    if not isinstance(reparsed, dict):
        return False, "packet must serialize to a top-level JSON object"
    array_path = _find_array_path(reparsed)
    if array_path:
        return False, f"array values are not allowed: {array_path}"
    if set(reparsed.keys()) != set(packet.keys()):
        return False, "packet must remain a single stable JSON object"

    missing = [field for field in STRICT_REQUIRED_RECOMMENDATION_FIELDS if field not in packet or not _has_meaningful_value(packet[field])]
    if missing:
        return False, f"missing required fields: {', '.join(missing)}"

    for field in ("action", "title", "rationale", "market_key"):
        value = packet.get(field)
        if not isinstance(value, str) or not value.strip():
            return False, f"{field} must be a non-empty string"

    priority = packet.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 100:
        return False, "priority must be an integer between 1 and 100"

    evidence = packet.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return False, "evidence must be a non-empty object"
    if not any(_has_meaningful_value(value) for value in evidence.values()):
        return False, "evidence must contain at least one non-empty value"

    proposed_change = packet.get("proposed_change")
    if not isinstance(proposed_change, dict) or not proposed_change:
        return False, "proposed_change must be a non-empty object"
    if not any(_has_meaningful_value(value) for value in proposed_change.values()):
        return False, "proposed_change must contain at least one non-empty value"

    for field in ("code_change", "variant_config"):
        if field not in packet or packet[field] in (None, ""):
            continue
        if not isinstance(packet[field], dict):
            return False, f"{field} must be an object when provided"

    variant_config = packet.get("variant_config")
    if isinstance(variant_config, dict) and "confirm_score_min" in variant_config:
        confirm_score_min, error = _coerce_confirm_score_min(variant_config.get("confirm_score_min"))
        if error:
            return False, error
        if confirm_score_min is not None and confirm_score_min < MIN_PAPER_CONFIRM_SCORE_MIN:
            return (
                False,
                f"confirm_score_min must be at least {MIN_PAPER_CONFIRM_SCORE_MIN:.2f} for paper-only variants",
            )

    return True, ""


def is_paper_only_suppression_recommendation(packet: dict[str, Any]) -> bool:
    """Return True when the packet intentionally avoids trade generation."""

    if not isinstance(packet, dict):
        return False

    action = packet.get("action")
    proposed_change = packet.get("proposed_change")
    if not isinstance(action, str) or not isinstance(proposed_change, dict):
        return False
    if action not in SUPPRESSION_ACTIONS:
        return False

    variant_config = packet.get("variant_config")
    if not isinstance(variant_config, dict) or variant_config.get("paper_only") is not True:
        return False
    entry_condition = proposed_change.get("entry_condition", "")
    return isinstance(entry_condition, str) and "complete valid json recommendation" in entry_condition.lower()


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_JSON = RUNS_DIR / "evolution_report.json"
REPORT_MD = RUNS_DIR / "evolution_report.md"
LEDGER_JSONL = RUNS_DIR / "evolution_ledger.jsonl"
WORKSPACE_PROBATION_STATUS = "workspace_applied_probation"
WORKSPACE_KEPT_STATUS = "workspace_kept"
LEGACY_PROBATION_STATUS = "merged_probation"
LEGACY_KEPT_STATUS = "kept"
PROBATION_STATUSES = {LEGACY_PROBATION_STATUS, WORKSPACE_PROBATION_STATUS}
RELEASE_SUCCESS_STATUSES = {"candidate_committed", "canary_running", "promoted"}
SUCCESS_STATUSES = {LEGACY_PROBATION_STATUS, LEGACY_KEPT_STATUS, WORKSPACE_PROBATION_STATUS, WORKSPACE_KEPT_STATUS, *RELEASE_SUCCESS_STATUSES}

ALLOWED_CATEGORIES = {
    "runtime_pipeline_integration",
    "public_data_adapter",
    "parser_improvement",
    "scanner_expansion",
    "paper_signal_variant",
    "paper_scoring_logic",
    "self_improvement_policy",
    "evolution_loop_improvement",
    "report_dashboard",
    "llm_prompt_state_packet",
    "quality_scoring",
    "read_only_route_intelligence",
    "tests_fixtures",
    "dependency_management",
}

IMPLEMENTATION_MODES = {
    "runtime_active",
    "paper_policy",
    "shadow_trial",
    "report_only",
}

CATEGORY_DEFAULT_IMPLEMENTATION_MODE = {
    "runtime_pipeline_integration": "runtime_active",
    "public_data_adapter": "runtime_active",
    "parser_improvement": "runtime_active",
    "scanner_expansion": "runtime_active",
    "quality_scoring": "runtime_active",
    "paper_scoring_logic": "paper_policy",
    "self_improvement_policy": "paper_policy",
    "paper_signal_variant": "shadow_trial",
    "evolution_loop_improvement": "runtime_active",
    "report_dashboard": "report_only",
    "llm_prompt_state_packet": "runtime_active",
    "read_only_route_intelligence": "report_only",
    "tests_fixtures": "report_only",
    "dependency_management": "runtime_active",
}

MARKET_EXPANSION_CATEGORIES = {
    "public_data_adapter",
    "parser_improvement",
    "scanner_expansion",
    "quality_scoring",
    "runtime_pipeline_integration",
}

FRONTIER_REQUIRED_CATEGORIES = {
    "public_data_adapter",
    "paper_scoring_logic",
    "self_improvement_policy",
    "evolution_loop_improvement",
}

CATEGORY_ALIASES = {
    "public-data adapter/parser improvements": "public_data_adapter",
    "runtime pipeline integration": "runtime_pipeline_integration",
    "runtime_pipeline": "runtime_pipeline_integration",
    "pipeline_integration": "runtime_pipeline_integration",
    "integration": "runtime_pipeline_integration",
    "wire_runtime": "runtime_pipeline_integration",
    "wire_into_runner": "runtime_pipeline_integration",
    "public_data_adapter_parser": "public_data_adapter",
    "public_data_adapter": "public_data_adapter",
    "adapter": "public_data_adapter",
    "parser": "parser_improvement",
    "parser_fix": "parser_improvement",
    "scanner_expansion_logic": "scanner_expansion",
    "scanner_expansion": "scanner_expansion",
    "signal_variant": "paper_signal_variant",
    "paper_only_signal_variants": "paper_signal_variant",
    "paper_scoring": "paper_scoring_logic",
    "paper_scoring_logic": "paper_scoring_logic",
    "paper_only_scoring": "paper_scoring_logic",
    "feature_flagged_paper_logic": "paper_scoring_logic",
    "self_improvement_policy": "self_improvement_policy",
    "policy_logic": "self_improvement_policy",
    "policy_ttl_revert": "self_improvement_policy",
    "evolution_loop": "evolution_loop_improvement",
    "code_evolution_repair": "evolution_loop_improvement",
    "self_building_loop": "evolution_loop_improvement",
    "reports": "report_dashboard",
    "dashboard": "report_dashboard",
    "report_dashboard": "report_dashboard",
    "llm_prompt": "llm_prompt_state_packet",
    "state_packet": "llm_prompt_state_packet",
    "quality_scoring": "quality_scoring",
    "market_expansion_quality_coverage": "scanner_expansion",
    "market_expansion": "scanner_expansion",
    "quality_coverage": "quality_scoring",
    "route_intelligence": "read_only_route_intelligence",
    "read_only_route_intelligence": "read_only_route_intelligence",
    "tests": "tests_fixtures",
    "fixtures": "tests_fixtures",
    "tests_fixtures": "tests_fixtures",
    "dependencies": "dependency_management",
    "dependency": "dependency_management",
    "dependency_management": "dependency_management",
    "python_dependencies": "dependency_management",
}

DEFAULT_ALLOWED_PATH_PREFIXES = [
    "src/",
    "tests/",
    "config/",
    "docs/",
    "README.md",
    "COST_AWARE_SWARM.md",
    "LLM_AGENT_BRIDGE.md",
    "requirements-llm.txt",
    "requirements-autonomous.txt",
]

DEFAULT_CATEGORY_FILES = {
    "runtime_pipeline_integration": [
        "src/radar_loop.py",
        "src/llm_bridge.py",
        "src/self_improvement.py",
        "src/route_resolver.py",
        "src/frontier_crypto_adapter.py",
        "tests/test_code_evolution.py",
    ],
    "public_data_adapter": [
        "src/frontier_crypto_adapter.py",
        "src/frontier_data_quality.py",
        "config/frontier_crypto_venues.example.json",
        "tests/test_frontier_crypto_adapter.py",
        "tests/test_frontier_data_quality.py",
    ],
    "parser_improvement": [
        "src/frontier_crypto_adapter.py",
        "src/frontier_data_quality.py",
        "tests/test_frontier_crypto_adapter.py",
        "tests/test_frontier_data_quality.py",
    ],
    "scanner_expansion": [
        "src/frontier_crypto_adapter.py",
        "src/frontier_data_quality.py",
        "config/frontier_crypto_venues.example.json",
        "tests/test_frontier_crypto_adapter.py",
        "tests/test_frontier_data_quality.py",
    ],
    "paper_signal_variant": [
        "src/signal_redesign.py",
        "tests/test_signal_redesign.py",
    ],
    "paper_scoring_logic": [
        "src/strategy_reliability.py",
        "src/frontier_crypto_adapter.py",
        "src/frontier_data_quality.py",
        "src/signal_redesign.py",
        "tests/test_strategy_reliability.py",
        "tests/test_frontier_data_quality.py",
    ],
    "self_improvement_policy": [
        "src/self_improvement.py",
        "src/signal_safety.py",
        "src/contextual_failure_filters.py",
        "tests/test_smart_failure_filters.py",
    ],
    "evolution_loop_improvement": [
        "src/code_evolution.py",
        "src/llm_swarm_runner.py",
        "src/llm_bridge.py",
        "tests/test_code_evolution.py",
    ],
    "report_dashboard": [
        "src/llm_bridge.py",
        "src/self_improvement.py",
        "src/frontier_quality_dashboard.py",
        "tests/test_code_evolution.py",
        "tests/test_frontier_quality_dashboard.py",
    ],
    "llm_prompt_state_packet": [
        "src/llm_bridge.py",
        "src/llm_swarm_runner.py",
        "tests/test_frontier_model_policy.py",
    ],
    "quality_scoring": [
        "src/frontier_crypto_adapter.py",
        "src/frontier_data_quality.py",
        "tests/test_frontier_data_quality.py",
        "tests/test_frontier_crypto_adapter.py",
    ],
    "read_only_route_intelligence": [
        "src/route_intelligence.py",
        "src/route_resolver.py",
        "tests/test_route_intelligence.py",
        "tests/test_route_resolver.py",
    ],
    "tests_fixtures": [
        "tests/test_code_evolution.py",
    ],
    "dependency_management": [
        "requirements-autonomous.txt",
        "requirements-llm.txt",
        "tests/test_code_evolution.py",
    ],
}

DEPENDENCY_MANIFESTS = {
    "requirements-autonomous.txt",
    "requirements-llm.txt",
}

RUNTIME_INTEGRATION_PATHS = {
    "src/radar_loop.py",
    "src/llm_bridge.py",
    "src/self_improvement.py",
    "src/self_improvement_open_pack.py",
    "src/code_evolution.py",
    "src/llm_swarm_runner.py",
    "src/llm_recommendation_ingestion.py",
    "src/llm_recommendation_parser.py",
    "src/llm_state_packet.py",
    "src/route_resolver.py",
    "src/route_intelligence.py",
    "src/frontier_crypto_adapter.py",
    "src/frontier_data_quality.py",
    "src/prediction_market_scanner.py",
    "src/signal_redesign.py",
    "src/strategy_reliability.py",
    "src/signal_safety.py",
    "src/contextual_failure_filters.py",
    "src/okx_signal_research.py",
    "src/okx_carry_economics.py",
}

RUNTIME_INTEGRATED_CATEGORIES = {
    "runtime_pipeline_integration",
    "public_data_adapter",
    "parser_improvement",
    "scanner_expansion",
    "paper_signal_variant",
    "paper_scoring_logic",
    "self_improvement_policy",
    "evolution_loop_improvement",
    "report_dashboard",
    "llm_prompt_state_packet",
    "quality_scoring",
    "read_only_route_intelligence",
}

PATH_ALIASES = {
    "code_evolution.py": "src/code_evolution.py",
    "self_improvement.py": "src/self_improvement.py",
    "llm_bridge.py": "src/llm_bridge.py",
    "llm_swarm.py": "src/llm_swarm_runner.py",
    "llm_swarm_runner.py": "src/llm_swarm_runner.py",
    "llm_state_packet.py": "src/llm_state_packet.py",
    "llm_swarm_packet.py": "src/llm_state_packet.py",
    "llm_recommendation_ingestion.py": "src/llm_recommendation_ingestion.py",
    "llm_recommendation_parser.py": "src/llm_recommendation_parser.py",
    "frontier_crypto_venues.py": "src/frontier_crypto_adapter.py",
    "src/radar/frontier_crypto_venues.py": "src/frontier_crypto_adapter.py",
    "src/radar/frontier_crypto_adapter.py": "src/frontier_crypto_adapter.py",
    "src/radar/frontier_data_quality.py": "src/frontier_data_quality.py",
    "src/radar/route_intelligence.py": "src/route_intelligence.py",
    "src/radar/route_resolver.py": "src/route_resolver.py",
    "src/frontier_crypto_venues.py": "src/frontier_crypto_adapter.py",
    "src/public_data_adapters/bitso_public.py": "src/frontier_crypto_adapter.py",
    "paper_trading.py": "src/paper_order_router.py",
    "paper_trading/scoring.py": "src/strategy_reliability.py",
    "paper_trading/signal_policy.py": "src/strategy_reliability.py",
    "src/agentic_trading_swarm/paper_scoring.py": "src/strategy_reliability.py",
    "src/agentic_trading_swarm/runtime_reports.py": "src/llm_bridge.py",
    "src/runtime_state_packet.py": "src/llm_bridge.py",
    "src/state_packet.py": "src/llm_bridge.py",
    "src/llm_state_packet.py": "src/llm_state_packet.py",
    "src/llm_swarm_packet.py": "src/llm_state_packet.py",
    "src/radar/llm_state_packet.py": "src/llm_bridge.py",
    "src/paper_scoring.py": "src/strategy_reliability.py",
    "src/runtime_reports.py": "src/llm_bridge.py",
    "src/runtime_reporting.py": "src/llm_bridge.py",
    "src/frontier_crypto_venue_map.py": "src/frontier_crypto_adapter.py",
    "src/crypto_venue_health.py": "src/frontier_crypto_adapter.py",
    "src/public_data_adapters/yahoo_proxy.py": "src/global_proxy_scanner.py",
    "src/prediction_markets_adapter.py": "src/prediction_market_scanner.py",
    "src/prediction_market_adapter.py": "src/prediction_market_scanner.py",
    "src/perp_funding_basis_adapter.py": "src/okx_signal_research.py",
    "src/okx_basis_adapter.py": "src/okx_signal_research.py",
    "src/paper_signal_policy.py": "src/strategy_reliability.py",
    "src/paper_signal_scoring.py": "src/strategy_reliability.py",
    "src/runtime_report.py": "src/llm_bridge.py",
    "src/radar/route_intelligence_report.py": "src/route_intelligence.py",
    "tests/test_frontier_crypto_venues.py": "tests/test_frontier_crypto_adapter.py",
    "tests/test_frontier_candidate_admission_scoring.py": "tests/test_frontier_data_quality.py",
    "tests/test_frontier_net_edge_candidate_admission.py": "tests/test_frontier_data_quality.py",
    "tests/test_runtime_state_packet_frontier_diagnostics.py": "tests/test_llm_state_packet.py",
    "tests/test_runtime_state_packet.py": "tests/test_llm_state_packet.py",
    "tests/test_llm_swarm_packet.py": "tests/test_llm_state_packet.py",
    "tests/test_llm_swarm.py": "tests/test_frontier_model_policy.py",
    "tests/test_route_intelligence_requirements.py": "tests/test_route_intelligence.py",
    "tests/test_prediction_market_classifier.py": "tests/test_prediction_market_scanner.py",
    "tests/test_bitso_public_depth.py": "tests/test_frontier_data_quality.py",
    "tests/test_paper_scoring.py": "tests/test_strategy_reliability.py",
    "tests/test_runtime_reports.py": "tests/test_llm_state_packet.py",
    "tests/test_runtime_reporting.py": "tests/test_llm_state_packet.py",
    "tests/test_yahoo_proxy_adapter.py": "tests/test_strategy_reliability.py",
    "tests/test_frontier_crypto_venue_map.py": "tests/test_frontier_crypto_adapter.py",
    "tests/test_crypto_venue_health.py": "tests/test_frontier_crypto_adapter.py",
    "tests/test_runtime_state_packet.py": "tests/test_llm_state_packet.py",
    "tests/test_state_packet.py": "tests/test_llm_state_packet.py",
    "tests/test_prediction_markets_adapter.py": "tests/test_prediction_market_scanner.py",
    "tests/test_prediction_market_adapter.py": "tests/test_prediction_market_scanner.py",
    "tests/test_perp_funding_basis_paper_policy.py": "tests/test_signal_redesign.py",
    "tests/test_perp_funding_basis_paper_policy_gate.py": "tests/test_signal_redesign.py",
    "tests/test_okx_basis_paper_policy.py": "tests/test_signal_redesign.py",
    "tests/test_paper_signal_scoring.py": "tests/test_strategy_reliability.py",
}

BLOCKED_PATH_PREFIXES = [
    ".git/",
    ".venv/",
    "runs/",
    "scripts/",
]

FORBIDDEN_ADDED_PATTERNS = [
    (re.compile(r"allow_live_trading[\"']?\s*[:=]\s*true", re.I), "enables_live_trading"),
    (re.compile(r"[\"']mode[\"']\s*:\s*[\"']live[\"']", re.I), "enables_live_mode"),
    (re.compile(r"max_live_notional_usd[\"']?\s*[:=]\s*(?!0(?:\.0)?\b)\d", re.I), "increases_live_notional"),
    (re.compile(r"[\"']spot_borrow[\"']\s*:\s*true", re.I), "enables_spot_borrow"),
    (re.compile(r"[\"']prediction_markets[\"']\s*:\s*true", re.I), "enables_prediction_market_account"),
    (re.compile(r"(?:\b|_)(api[_-]?key|api[_-]?token|secret|password|private[_-]?key|credential(?:s)?)\b\s*[:=]", re.I), "touches_credentials"),
    (re.compile(r"os\.environ(?:\.get)?\([\"'][A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*[\"']", re.I), "touches_credentials"),
    (re.compile(r"\b(create_order|place_order|submit_order|cancel_order|withdraw|transfer_funds)\b", re.I), "broker_write_or_order_api"),
    (re.compile(r"\b(delete\s+from|drop\s+table|truncate\s+table|vacuum\s+full)\b", re.I), "destructive_database_action"),
    (re.compile(r"\b(shutil\.rmtree|os\.remove|os\.unlink|Remove-Item|rm\s+-rf)\b", re.I), "destructive_filesystem_action"),
    (re.compile(r"\b(pip\s+install|npm\s+install|winget\s+install|choco\s+install)\b", re.I), "installer_command_in_code"),
    (re.compile(r"\b(Register-ScheduledTask|schtasks|crontab|New-Service|Set-Service)\b", re.I), "startup_or_system_task"),
]

RUNTIME_FORBIDDEN_REASONS = {
    "enables_live_trading",
    "enables_live_mode",
    "increases_live_notional",
    "enables_spot_borrow",
    "enables_prediction_market_account",
    "touches_credentials",
    "broker_write_or_order_api",
}

ALWAYS_FORBIDDEN_REASONS = {
    "destructive_database_action",
    "destructive_filesystem_action",
    "startup_or_system_task",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _cfg(settings: dict) -> dict:
    defaults = {
        "enabled": True,
        "auto_merge_paper_only": True,
        "max_auto_merges_per_loop": 12,
        "require_frontier_model": False,
        "required_model": "openai/gpt-5.6-sol",
        "min_priority": 50,
        "run_full_regression": True,
        "probation_loops": 1,
        "rollback_on_health_failure": True,
        "generate_patch_when_missing": True,
        "repair_invalid_patch_format": True,
        "repair_patch_when_apply_fails": True,
        "repair_patch_when_sandbox_fails": True,
        "patch_repair_attempts": 2,
        "repair_unknown_paths": True,
        "validate_test_commands": True,
        "reject_orphan_helpers": True,
        "patch_generation_max_file_chars": 16000,
        "patch_generation_max_output_tokens": 12000,
        "patch_generation_timeout_seconds": 90,
        "sandbox_timeout_seconds": 120,
        "install_changed_python_dependencies": True,
        "standard_patch_tier": "fast",
        "repair_patch_tier": "fast",
        "high_quality_patch_tier": "standard",
        "high_quality_patch_min_score": 90,
        "frontier_patch_tier": "frontier",
        "frontier_required_categories": [
            "evolution_loop_improvement",
        ],
        "frontier_required_priority": 98,
        "frontier_waste_guard_enabled": True,
        "frontier_waste_guard_recent": 12,
        "frontier_waste_guard_after": 8,
        "frontier_waste_guard_min_quality": 55,
        "duplicate_failure_suppression_enabled": True,
        "duplicate_failure_suppression_recent": 80,
        "duplicate_failure_suppression_after": 3,
        "allowed_categories": sorted(ALLOWED_CATEGORIES),
        "allowed_path_prefixes": DEFAULT_ALLOWED_PATH_PREFIXES,
        "git_release_enabled": False,
        "run_candidate_canary": False,
        "promote_candidate_after_canary": False,
        "release_worktree_dir": str(RUNS_DIR / "evolution_worktrees"),
        "canary_timeout_seconds": 180,
        "canary_max_latency_seconds": 180.0,
    }
    return {**defaults, **settings.get("code_evolution", {})}


def _normalize_category(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return CATEGORY_ALIASES.get(raw, raw)


def _normalize_implementation_mode(value: object) -> str | None:
    if value in (None, "", []):
        return None
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return raw


def _implementation_mode(payload: dict, category: str) -> str:
    raw = (
        payload.get("implementation_mode")
        or _code_change(payload).get("implementation_mode")
        or payload.get("mode")
    )
    explicit = _normalize_implementation_mode(raw)
    if explicit:
        return explicit
    if _is_market_expansion_proposal(payload, category):
        return "runtime_active"
    return CATEGORY_DEFAULT_IMPLEMENTATION_MODE.get(category, "report_only")


def _is_market_expansion_proposal(payload: dict, category: str) -> bool:
    if category in MARKET_EXPANSION_CATEGORIES:
        return True
    text = json.dumps(payload, sort_keys=True).lower()
    return "market expansion" in text or any(
        token in text
        for token in (
            "depth enrichment",
            "candidate cap",
            "candidate review",
            "starved venue",
            "quality coverage",
            "new markets tested",
            "public adapter",
            "frontier venue",
        )
    )


def _frontier_required(payload: dict, category: str, implementation_mode: str, preflight: dict | None = None, cfg: dict | None = None) -> bool:
    cfg = cfg or {}
    if not cfg.get("require_frontier_model", True):
        return False
    if bool(payload.get("require_frontier_model") or _code_change(payload).get("require_frontier_model")):
        return True
    if implementation_mode == "shadow_trial":
        return False
    required_categories = set(cfg.get("frontier_required_categories", sorted(FRONTIER_REQUIRED_CATEGORIES)))
    if category in required_categories:
        return True
    priority = int(payload.get("priority", 0) or 0)
    if implementation_mode in {"runtime_active", "paper_policy"} and priority >= int(cfg.get("frontier_required_priority", 90)):
        target_count = len((preflight or {}).get("target_files", []) or [])
        return target_count >= 3
    return False


def _patch_generation_tier(payload: dict, settings: dict, preflight: dict | None = None) -> str:
    cfg = _cfg(settings)
    category = _normalize_category(_field(payload, "change_category", "category"))
    mode = _implementation_mode(payload, category)
    if _frontier_required(payload, category, mode, preflight=preflight, cfg=cfg):
        return str(cfg.get("frontier_patch_tier", "frontier"))
    scorecard = (preflight or {}).get("quality_scorecard") or {}
    try:
        quality_score = float(scorecard.get("proposal_quality_score") or 0)
    except (TypeError, ValueError):
        quality_score = 0.0
    high_quality_categories = {
        "runtime_pipeline_integration",
        "scanner_expansion",
        "public_data_adapter",
        "parser_improvement",
        "quality_scoring",
        "paper_scoring_logic",
        "self_improvement_policy",
        "evolution_loop_improvement",
    }
    if category in high_quality_categories and quality_score >= float(cfg.get("high_quality_patch_min_score", 90)):
        return str(cfg.get("high_quality_patch_tier", "standard"))
    return str(cfg.get("standard_patch_tier", "standard"))


def _patch_reasoning_effort(tier_name: str) -> str:
    if tier_name == "frontier":
        return "high"
    if tier_name in {"standard", "codex"}:
        return "medium"
    return "low"


def _proposal_id(source_recommendation_id: str | None, payload: dict) -> str:
    seed = json.dumps({"source": source_recommendation_id, "payload": payload}, sort_keys=True)
    return f"code_evolution:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _model_name(payload: dict) -> str | None:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    return model.get("name") or payload.get("model_name")


def _model_tier(payload: dict) -> str | None:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    return model.get("tier") or payload.get("model_tier")


def _code_change(payload: dict) -> dict:
    return payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}


def _field(payload: dict, *names: str) -> object:
    code_change = _code_change(payload)
    for name in names:
        if payload.get(name) not in (None, "", []):
            return payload.get(name)
        if code_change.get(name) not in (None, "", []):
            return code_change.get(name)
    return None


def _proposal_evidence(payload: dict) -> dict:
    evidence: dict = {}
    raw_top = payload.get("evidence")
    raw_nested = _code_change(payload).get("evidence")
    if isinstance(raw_nested, dict):
        nested = raw_nested
    elif isinstance(raw_nested, str) and raw_nested.strip():
        nested = {"summary": raw_nested.strip(), "source": "code_change.evidence"}
    else:
        nested = {}
    if isinstance(raw_top, dict):
        top = raw_top
    elif isinstance(raw_top, str) and raw_top.strip():
        top = {"summary": raw_top.strip(), "source": "payload.evidence"}
    else:
        top = {}
    evidence.update(nested)
    evidence.update(top)
    return evidence


def _frontier_reason(payload: dict) -> str | None:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    return (
        payload.get("frontier_escalation_reason")
        or _code_change(payload).get("frontier_escalation_reason")
        or model.get("frontier_escalation_reason")
    )


def _extract_patch(payload: dict) -> str:
    for key in ("unified_diff", "patch", "diff"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_fence(value)
    code_change = payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}
    for key in ("unified_diff", "patch", "diff"):
        value = code_change.get(key)
        if isinstance(value, str) and value.strip():
            return _strip_fence(value)
    return ""


def _strip_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip() + ("\n" if text.strip() else "")


def changed_files_from_diff(diff_text: str) -> list[str]:
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            raw = line[4:].strip().split("\t", 1)[0]
            if raw == "/dev/null":
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            path = _canonical_path(raw)
            if path:
                files.add(path)
    return sorted(files)


def _extract_apply_patch_block(text: str) -> str:
    begin = text.find("*** Begin Patch")
    end = text.find("*** End Patch")
    if begin < 0 or end < begin:
        return ""
    return text[begin : end + len("*** End Patch")]


def _apply_patch_to_context_diff(patch_text: str) -> str:
    """Convert Codex apply_patch text into context-applied diff form.

    The autonomous builder often returns the same patch grammar used by the
    local apply_patch tool. The code-evolution sandbox expects a unified diff,
    but its internal applier can already apply context hunks when line numbers
    drift. This conversion preserves the files and hunk bodies so those patches
    can use the same safety/test path instead of being discarded as malformed.
    """

    block = _extract_apply_patch_block(patch_text)
    if not block:
        return patch_text

    lines = block.splitlines()
    out: list[str] = []
    index = 0
    converted = False

    def append_update(path: str, hunks: list[list[str]]) -> None:
        nonlocal converted
        if not path or not hunks:
            return
        out.extend([f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"])
        for hunk in hunks:
            if hunk:
                out.append("@@ -1 +1 @@")
                out.extend(hunk)
        converted = True

    def append_add(path: str, added_lines: list[str]) -> None:
        nonlocal converted
        if not path:
            return
        out.extend(
            [
                f"diff --git a/{path} b/{path}",
                "new file mode 100644",
                "--- /dev/null",
                f"+++ b/{path}",
                "@@ -0,0 +1 @@",
            ]
        )
        out.extend(added_lines)
        converted = True

    while index < len(lines):
        line = lines[index]
        if line.startswith("*** Update File: "):
            path = _canonical_path(line.split(":", 1)[1].strip())
            index += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while index < len(lines):
                body = lines[index]
                if body.startswith("*** "):
                    break
                if body.startswith("@@"):
                    if current:
                        hunks.append(current)
                    current = []
                    index += 1
                    continue
                if body[:1] in {" ", "+", "-"}:
                    current.append(body)
                index += 1
            if current:
                hunks.append(current)
            append_update(path, hunks)
            continue
        if line.startswith("*** Add File: "):
            path = _canonical_path(line.split(":", 1)[1].strip())
            index += 1
            added: list[str] = []
            while index < len(lines):
                body = lines[index]
                if body.startswith("*** "):
                    break
                if body.startswith("+"):
                    added.append(body)
                index += 1
            append_add(path, added)
            continue
        index += 1

    return ("\n".join(out).rstrip() + "\n") if converted else patch_text


def rewrite_diff_paths(diff_text: str) -> str:
    """Canonicalize generated diff headers before sandboxing.

    Models often produce otherwise usable patches against root-level module
    names such as ``code_evolution.py`` or ``llm_recommendation_ingestion.py``.
    The preflight path repair already maps those into ``src/``; this function
    applies the same repair to the actual unified diff so the patch is not
    discarded after a valid preflight repair.
    """

    diff_text = _apply_patch_to_context_diff(diff_text)
    if not diff_text.strip():
        return diff_text

    def rewrite_token(token: str) -> str:
        if token == "/dev/null":
            return token
        prefix = ""
        body = token
        if token.startswith("a/") or token.startswith("b/"):
            prefix, body = token[:2], token[2:]
        canonical = _canonical_path(body)
        return f"{prefix}{canonical}" if canonical else token

    def rewrite_header_path(value: str) -> str:
        path, sep, suffix = value.partition("\t")
        return rewrite_token(path) + (sep + suffix if sep else "")

    repaired: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                parts[2] = rewrite_token(parts[2])
                parts[3] = rewrite_token(parts[3])
                line = " ".join(parts)
        elif line.startswith("--- ") or line.startswith("+++ "):
            line = line[:4] + rewrite_header_path(line[4:].strip())
        elif line.startswith("rename from "):
            line = "rename from " + rewrite_header_path(line[len("rename from ") :].strip())
        elif line.startswith("rename to "):
            line = "rename to " + rewrite_header_path(line[len("rename to ") :].strip())
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if diff_text.endswith("\n") else "")


def _prefix_diff_paths(diff_text: str, prefix: str) -> str:
    prefix = prefix.replace("\\", "/").strip("/")
    if not prefix or not diff_text.strip():
        return diff_text

    def prefix_token(token: str) -> str:
        if token == "/dev/null":
            return token
        token_prefix = ""
        body = token
        if token.startswith("a/") or token.startswith("b/"):
            token_prefix, body = token[:2], token[2:]
        if body.startswith(f"{prefix}/"):
            return token
        return f"{token_prefix}{prefix}/{body}"

    def prefix_header_path(value: str) -> str:
        path, sep, suffix = value.partition("\t")
        return prefix_token(path) + (sep + suffix if sep else "")

    repaired: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                parts[2] = prefix_token(parts[2])
                parts[3] = prefix_token(parts[3])
                line = " ".join(parts)
        elif line.startswith("--- ") or line.startswith("+++ "):
            line = line[:4] + prefix_header_path(line[4:].strip())
        elif line.startswith("rename from "):
            line = "rename from " + prefix_header_path(line[len("rename from ") :].strip())
        elif line.startswith("rename to "):
            line = "rename to " + prefix_header_path(line[len("rename to ") :].strip())
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if diff_text.endswith("\n") else "")


def _git_toplevel(root: pathlib.Path, timeout: int = 30) -> pathlib.Path | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    top = completed.stdout.strip()
    return pathlib.Path(top) if top else None


def _patch_apply_location(root: pathlib.Path, diff_text: str, timeout: int) -> tuple[pathlib.Path, str, str | None]:
    top = _git_toplevel(root, timeout=timeout)
    if top is None:
        return root, diff_text, None
    try:
        root_resolved = root.resolve()
        top_resolved = top.resolve()
    except OSError:
        return root, diff_text, None
    if root_resolved == top_resolved:
        return root, diff_text, None
    try:
        prefix = root_resolved.relative_to(top_resolved).as_posix()
    except ValueError:
        return root, diff_text, None
    return top, _prefix_diff_paths(diff_text, prefix), prefix


def _looks_like_unified_diff(diff_text: str) -> bool:
    text = diff_text.lstrip()
    return text.startswith("diff --git ") or (
        "\n--- " in f"\n{text}" and "\n+++ " in f"\n{text}" and "\n@@ " in f"\n{text}"
    )


def _normalize_path(raw: str) -> str:
    path = raw.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return ""
    parts = pathlib.PurePosixPath(path).parts
    if ".." in parts:
        return ""
    return str(pathlib.PurePosixPath(path))


def _canonical_path(path: str) -> str:
    norm = _normalize_path(path)
    if not norm:
        return ""
    aliased = PATH_ALIASES.get(norm)
    if aliased:
        return aliased
    if "/" not in norm and norm.endswith(".py"):
        return f"src/{norm}"
    return norm


def _path_creation_allowed(path: str, category: str) -> bool:
    if path.startswith("tests/fixtures/"):
        return pathlib.PurePosixPath(path).suffix.lower() in {".json", ".jsonl", ".csv", ".txt", ".yaml", ".yml"}
    if path.startswith("tests/test_") and path.endswith(".py"):
        return True
    if path.startswith("src/") and path.endswith(".py") and category in {
        "runtime_pipeline_integration",
        "parser_improvement",
        "evolution_loop_improvement",
        "llm_prompt_state_packet",
        "report_dashboard",
        "tests_fixtures",
    }:
        return True
    return False


def _semantic_target_files(raw: str, category: str, payload: dict) -> list[str]:
    text = " ".join(
        [
            str(raw or ""),
            str(payload.get("title") or ""),
            str(_field(payload, "proposed_change", "rationale") or ""),
        ]
    ).lower()
    if any(token in text for token in ("prediction", "kalshi", "polymarket", "event market")):
        return [
            "src/prediction_market_scanner.py",
            "tests/test_prediction_market_scanner.py",
        ]
    crypto_market_terms = (
        "crypto",
        "stablecoin",
        "token",
        "perp",
        "frontier scanner",
        "frontier crypto",
        "depth enrichment",
        "fiat quote",
        "quote normalization",
    )
    if any(token in text for token in crypto_market_terms):
        return [
            "src/frontier_crypto_adapter.py",
            "src/frontier_data_quality.py",
            "tests/test_frontier_crypto_adapter.py",
            "tests/test_frontier_data_quality.py",
        ]
    global_public_data_terms = (
        "public data",
        "public no-key",
        "no-key",
        "official docs",
        "api docs",
        "endpoint",
        "feed",
        "ticker",
        "quote",
        "settlement",
        "auction",
        "instrument list",
        "symbol list",
        "market surface",
        "exchange",
        "venue",
    )
    if category == "public_data_adapter" and any(token in text for token in global_public_data_terms):
        return [
            "src/research_worker.py",
            "src/llm_bridge.py",
            "tests/test_research_worker.py",
        ]
    if any(token in text for token in ("paper scoring", "signal scoring", "score_adjustment", "quarantine", "shadow_filtered")):
        return [
            "src/strategy_reliability.py",
            "src/paper_order_router.py",
            "tests/test_strategy_reliability.py",
            "tests/test_paper_order_router.py",
        ]
    if any(token in text for token in ("route capability", "conditional route", "borrow", "spot_borrow", "route blocker")):
        return [
            "src/route_intelligence.py",
            "src/route_resolver.py",
            "tests/test_route_intelligence.py",
            "tests/test_route_resolver.py",
        ]
    if any(token in text for token in ("hunter directive", "self-improvement", "self improvement", "exploit directive")):
        return [
            "src/self_improvement.py",
            "src/signal_safety.py",
            "tests/test_smart_failure_filters.py",
        ]
    if category in DEFAULT_CATEGORY_FILES:
        normalized_raw = str(raw).replace("\\", "/").strip()
        path_like = bool(
            re.search(r"\b(?:src|tests|config|docs)/", normalized_raw)
            or re.search(r"\.(?:py|md|json|yaml|yml|toml|txt)$", normalized_raw, re.I)
        )
        if not path_like and any(word in text for word in ("module", "policy", "scanner", "adapter", "report", "runtime", "test")):
            return DEFAULT_CATEGORY_FILES.get(category, [])[:4]
    return []


def _target_files(payload: dict) -> list[str]:
    expected_files = _field(payload, "expected_files", "files_expected_to_change") or []
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        return []
    return [str(item) for item in expected_files]


def _raw_test_commands(payload: dict) -> list[str]:
    raw_tests = _field(payload, "tests_to_run", "tests") or []
    if isinstance(raw_tests, str):
        raw_tests = [raw_tests]
    if not isinstance(raw_tests, list):
        return []
    return [str(item) for item in raw_tests[:5]]


def preflight_proposal(payload: dict, settings: dict, root: pathlib.Path = ROOT) -> dict:
    cfg = _cfg(settings)
    category = _normalize_category(_field(payload, "change_category", "category"))
    implementation_mode = _implementation_mode(payload, category)
    target_files = []
    path_repairs = []
    invalid_targets = []
    used_default_targets = False
    for raw in _target_files(payload):
        semantic_targets = _semantic_target_files(raw, category, payload)
        if semantic_targets:
            added_semantic: list[str] = []
            for semantic in semantic_targets:
                if not _path_blocked(semantic, cfg) and ((root / semantic).exists() or semantic in DEFAULT_CATEGORY_FILES.get(category, [])):
                    target_files.append(semantic)
                    added_semantic.append(semantic)
            if added_semantic:
                path_repairs.append({"from": raw, "to": sorted(dict.fromkeys(added_semantic))})
                continue
        norm = _normalize_path(raw)
        canonical = _canonical_path(raw)
        if canonical and canonical != norm:
            path_repairs.append({"from": norm or raw, "to": canonical})
        if not canonical or _path_blocked(canonical, cfg):
            invalid_targets.append(raw)
            continue
        if not (root / canonical).exists() and canonical not in DEFAULT_CATEGORY_FILES.get(category, []):
            if not _path_creation_allowed(canonical, category):
                invalid_targets.append(raw)
                continue
        target_files.append(canonical)
    if not target_files:
        semantic_defaults = _semantic_target_files("", category, payload)
        if semantic_defaults:
            used_default_targets = True
            target_files = [
                rel
                for rel in semantic_defaults
                if not _path_blocked(rel, cfg) and ((root / rel).exists() or rel in DEFAULT_CATEGORY_FILES.get(category, []))
            ][:8]
            if target_files:
                path_repairs.append({"from": "repo_aware_preflight", "to": target_files})
        if not target_files:
            used_default_targets = True
            target_files = [
                rel
                for rel in DEFAULT_CATEGORY_FILES.get(category, [])
                if not _path_blocked(rel, cfg) and (root / rel).exists()
            ][:8]
    test_issues = []
    test_repairs = []
    parsed_tests = []
    for command in _raw_test_commands(payload):
        parsed = _safe_test_command(command, root=root)
        if parsed:
            parsed_tests.append(parsed)
        else:
            repaired = _repair_test_command(command, category, root=root)
            if repaired:
                parsed_tests.append(repaired)
                test_repairs.append({"from": command, "to": repaired})
            else:
                test_issues.append(command)
    canonical_tests = _canonical_test_commands_for_files(target_files, category, root=root)
    if canonical_tests:
        existing = {tuple(row) for row in parsed_tests}
        added = [row for row in canonical_tests if tuple(row) not in existing]
        if added:
            parsed_tests.extend(added)
        if test_issues and not any(_unsafe_test_command(issue) for issue in test_issues):
            test_repairs.append({"from": "invalid_or_stale_model_tests", "to": added or canonical_tests})
            test_issues = []
        elif _raw_test_commands(payload) and added:
            test_repairs.append({"from": "model_tests_augmented_with_canonical_tests", "to": added})
    quality_scorecard = _proposal_quality_scorecard(
        payload,
        category=category,
        implementation_mode=implementation_mode,
        target_files=sorted(dict.fromkeys(target_files))[:8],
        invalid_targets=invalid_targets,
        path_repairs=path_repairs,
        test_issues=test_issues,
        test_repairs=test_repairs,
        used_default_targets=used_default_targets,
    )
    return {
        "category": category,
        "implementation_mode": implementation_mode,
        "implementation_mode_valid": implementation_mode in IMPLEMENTATION_MODES,
        "target_files": sorted(dict.fromkeys(target_files))[:8],
        "path_repairs": path_repairs,
        "invalid_targets": invalid_targets,
        "test_issues": test_issues,
        "test_repairs": test_repairs,
        "parsed_tests": parsed_tests,
        "quality_scorecard": quality_scorecard,
        "checked_at": _utc_now(),
    }


def _proposal_quality_scorecard(
    payload: dict,
    *,
    category: str,
    implementation_mode: str,
    target_files: list[str],
    invalid_targets: list[str],
    path_repairs: list[dict],
    test_issues: list[str],
    test_repairs: list[dict],
    used_default_targets: bool,
) -> dict:
    runtime_status = _runtime_integration_status(payload, category, target_files)
    implementation_mode_valid = implementation_mode in IMPLEMENTATION_MODES
    category_valid = category in ALLOWED_CATEGORIES
    if invalid_targets:
        target_path_status = "invalid"
    elif path_repairs:
        target_path_status = "repaired"
    elif used_default_targets:
        target_path_status = "defaulted"
    elif target_files:
        target_path_status = "valid"
    else:
        target_path_status = "missing"

    if test_issues:
        test_command_status = "invalid"
    elif test_repairs:
        test_command_status = "repaired"
    elif _raw_test_commands(payload):
        test_command_status = "valid"
    else:
        test_command_status = "default_regression"

    score = 100
    if target_path_status == "invalid":
        score -= 45
    elif target_path_status == "missing":
        score -= 35
    elif target_path_status == "defaulted":
        score -= 12
    elif target_path_status == "repaired":
        score -= 5
    if test_command_status == "invalid":
        score -= 35
    elif test_command_status == "default_regression":
        score -= 5
    if runtime_status in {"missing", "integration_claim_without_target"}:
        score -= 45
    elif runtime_status == "test_or_fixture_only":
        score -= 10
    if not implementation_mode_valid:
        score -= 40
    if not category_valid:
        score -= 55

    reject_status = None
    if not category_valid:
        reject_status = "rejected_preflight_invalid_category"
    elif not implementation_mode_valid:
        reject_status = "rejected_preflight_invalid_implementation_mode"
    elif target_path_status in {"invalid", "missing"}:
        reject_status = "rejected_preflight_invalid_target"
    elif test_command_status == "invalid":
        reject_status = "rejected_preflight_invalid_tests"
    elif runtime_status in {"missing", "integration_claim_without_target"}:
        reject_status = "rejected_preflight_no_runtime_integration"

    return {
        "proposal_quality_score": max(0, min(100, score)),
        "category_valid": category_valid,
        "implementation_mode": implementation_mode,
        "implementation_mode_valid": implementation_mode_valid,
        "target_path_status": target_path_status,
        "test_command_status": test_command_status,
        "runtime_integration_status": runtime_status,
        "expected_behavior_change": _expected_behavior_change(payload, runtime_status),
        "reject_before_model_call": bool(reject_status),
        "preflight_reject_status": reject_status,
        "repair_attempted": bool(path_repairs or test_repairs),
        "repair_successful": bool((path_repairs or test_repairs) and not invalid_targets and not test_issues),
    }


def _runtime_integration_status(payload: dict, category: str, target_files: list[str]) -> str:
    if category == "tests_fixtures":
        return "test_or_fixture_only"
    if category not in RUNTIME_INTEGRATED_CATEGORIES:
        return "not_required"
    runtime_targets = [
        path
        for path in target_files
        if path in RUNTIME_INTEGRATION_PATHS or path.startswith("config/")
    ]
    if runtime_targets:
        return "integrated"
    text = json.dumps(payload, sort_keys=True).lower()
    integration_terms = (
        "wire",
        "integrat",
        "runner",
        "llm packet",
        "state packet",
        "report generation",
        "normal radar",
        "runtime",
    )
    if category == "report_dashboard" and any(term in text for term in integration_terms):
        return "integration_claim_without_target"
    return "missing"


def _actual_runtime_integration_status(payload: dict, category: str, changed_files: list[str]) -> str:
    status = _runtime_integration_status(payload, category, changed_files)
    if status == "integrated":
        return status
    changed_source_files = [
        path
        for path in changed_files
        if path.startswith("src/") and path.endswith(".py")
    ]
    if changed_source_files and category in RUNTIME_INTEGRATED_CATEGORIES:
        return "changed_source_without_runtime_wiring"
    return status


def _expected_behavior_change(payload: dict, runtime_status: str) -> str:
    proposed = str(_field(payload, "proposed_change", "expected_paper_only_impact") or payload.get("rationale") or "").strip()
    if runtime_status == "integrated":
        return proposed[:240] or "runtime_integrated_behavior_change"
    if runtime_status == "test_or_fixture_only":
        return "test_or_fixture_change_only"
    if runtime_status == "integration_claim_without_target":
        return "integration_claim_missing_runtime_target"
    return "missing_runtime_integration"


def _repair_test_command(command: str, category: str, root: pathlib.Path = ROOT) -> list[str] | None:
    cleaned = command.strip().replace("\\", "/")
    forbidden = [";", "&", "|", ">", "<", "`", "$("]
    if any(token in cleaned for token in forbidden):
        return None
    compileall = _safe_compileall_command(command, root=root)
    if compileall:
        return compileall
    repaired_paths: list[str] = []
    for token in cleaned.split():
        path_part = token.split("::", 1)[0]
        norm = _normalize_path(path_part)
        canonical = _canonical_path(norm)
        if canonical != norm and canonical.startswith("tests/") and canonical.endswith(".py") and (root / canonical).exists():
            repaired_paths.append(canonical)
    mentions_known_alias = any(alias in cleaned for alias in PATH_ALIASES if alias.startswith("tests/"))
    mentions_test_path = bool(re.search(r"\btests/[A-Za-z0-9_./:-]+\.py", cleaned))
    if not repaired_paths and "pytest" in cleaned.lower() and (mentions_known_alias or not mentions_test_path):
        repaired_paths = [
            rel
            for rel in DEFAULT_CATEGORY_FILES.get(category, [])
            if rel.startswith("tests/") and rel.endswith(".py") and (root / rel).exists()
        ][:2]
    if not repaired_paths and "pytest" in cleaned.lower() and category not in {"report_dashboard", "tests_fixtures"}:
        repaired_paths = [
            rel
            for rel in DEFAULT_CATEGORY_FILES.get(category, [])
            if rel.startswith("tests/") and rel.endswith(".py") and (root / rel).exists()
        ][:3]
    if not repaired_paths:
        return None
    return [sys.executable, "-m", "unittest", *sorted(dict.fromkeys(repaired_paths))[:3]]


def _unsafe_test_command(command: str) -> bool:
    cleaned = command.strip()
    forbidden = [";", "&", "|", ">", "<", "`", "$("]
    return any(token in cleaned for token in forbidden)


def _canonical_test_commands_for_files(files: list[str], category: str, root: pathlib.Path = ROOT) -> list[list[str]]:
    tests: list[str] = []
    for rel in files:
        if rel.startswith("tests/") and rel.endswith(".py") and (
            (root / rel).exists() or _path_creation_allowed(rel, "tests_fixtures")
        ):
            tests.append(rel)
    category_tests = [
        rel
        for rel in DEFAULT_CATEGORY_FILES.get(category, [])
        if rel.startswith("tests/") and rel.endswith(".py") and (root / rel).exists()
    ]
    if not tests:
        tests.extend(category_tests[:3])
    else:
        tests.extend(category_tests[:2])
    deduped = sorted(dict.fromkeys(tests))[:4]
    if not deduped:
        return []
    return [[sys.executable, "-m", "unittest", *deduped]]


def _added_text(diff_text: str) -> str:
    lines = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++ "):
            lines.append(line[1:])
    return "\n".join(lines)


def _forbidden_reasons_from_diff(diff_text: str) -> list[str]:
    reasons: set[str] = set()
    current_path = ""
    added_by_path: dict[str, list[str]] = {}
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            current_path = _canonical_path(_diff_file_path(raw[4:].strip()))
            added_by_path.setdefault(current_path, [])
            continue
        if raw.startswith("+") and not raw.startswith("+++ "):
            added_by_path.setdefault(current_path, []).append(raw[1:])

    for path, lines in added_by_path.items():
        if not lines:
            continue
        text = "\n".join(lines)
        test_or_doc = _is_test_or_doc_path(path)
        for pattern, reason in FORBIDDEN_ADDED_PATTERNS:
            if not pattern.search(text):
                continue
            if test_or_doc and reason in RUNTIME_FORBIDDEN_REASONS:
                continue
            reasons.add(reason)
    return sorted(reasons)


def _is_test_or_doc_path(path: str) -> bool:
    return (
        path.startswith("tests/")
        or path.startswith("docs/")
        or path in {"README.md", "COST_AWARE_SWARM.md", "LLM_AGENT_BRIDGE.md"}
    )


def _safety_decision(reasons: list[str], patch_generation: dict | None = None, preflight: dict | None = None) -> str:
    if not reasons:
        return "auto_allowed"
    if _patch_generation_unavailable_reason(patch_generation):
        return "patch_generation_unavailable_retry_later"
    text = json.dumps(patch_generation or {}, sort_keys=True).lower()
    if "timeout" in text or "timed out" in text:
        return "patch_generation_timeout"
    scorecard = (preflight or {}).get("quality_scorecard") or {}
    if scorecard.get("reject_before_model_call") and scorecard.get("preflight_reject_status"):
        return str(scorecard["preflight_reject_status"])
    if any(reason.startswith("category_not_allowed:") for reason in reasons):
        return "rejected_preflight_invalid_category"
    if "invalid_implementation_mode" in reasons:
        return "rejected_preflight_invalid_implementation_mode"
    if "invalid_target_files" in reasons:
        return "rejected_preflight_invalid_target"
    if preflight and preflight.get("test_issues"):
        return "rejected_preflight_invalid_tests"
    if "no_runtime_integration_target" in reasons:
        return "rejected_preflight_no_runtime_integration"
    if "invalid_patch_format" in reasons:
        return "invalid_patch_format"
    if "missing_unified_diff" in reasons and ("no_changed_files" in reasons or (patch_generation or {}).get("status")):
        return "patch_generation_failed"
    if "no_changed_files" in reasons and len(reasons) <= 1:
        return "no_changed_files"
    safety_reasons = {
        "enables_live_trading",
        "enables_live_mode",
        "increases_live_notional",
        "enables_spot_borrow",
        "enables_prediction_market_account",
        "touches_credentials",
        "broker_write_or_order_api",
        "destructive_database_action",
        "destructive_filesystem_action",
        "installer_command_in_code",
        "startup_or_system_task",
    }
    if any(reason in safety_reasons or reason.startswith("path_not_allowed:") for reason in reasons):
        return "blocked_safety"
    return "blocked_human_review"


def _patch_generation_unavailable_reason(patch_generation: dict | None) -> str | None:
    status = str((patch_generation or {}).get("status") or "").lower()
    if not status:
        return None
    if "fallback_no_cost" in status:
        return "model_calls_disabled"
    if "fallback_missing_provider_key" in status:
        return "missing_provider_key"
    if "agent_budget_guard" in status or "global_budget_guard" in status:
        return "budget_guard"
    if "insufficient_quota" in status or "429" in status:
        return "quota_429"
    if "connection error" in status or "api connection" in status:
        return "connection_error"
    if "rate_limit" in status or "rate limit" in status:
        return "rate_limit"
    return None


def validate_and_scan(
    payload: dict,
    diff_text: str,
    settings: dict,
    *,
    patch_generation: dict | None = None,
    preflight: dict | None = None,
) -> dict:
    diff_text = rewrite_diff_paths(diff_text)
    cfg = _cfg(settings)
    category = _normalize_category(_field(payload, "change_category", "category"))
    implementation_mode = _implementation_mode(payload, category)
    priority = int(payload.get("priority", 0) or 0)
    changed_files = changed_files_from_diff(diff_text)
    reasons: list[str] = []
    unavailable_reason = _patch_generation_unavailable_reason(patch_generation)

    if not cfg.get("enabled", True):
        reasons.append("code_evolution_disabled")
    if priority < int(cfg.get("min_priority", 80)):
        reasons.append("priority_below_minimum")
    if category not in set(cfg.get("allowed_categories", sorted(ALLOWED_CATEGORIES))):
        reasons.append(f"category_not_allowed:{category or 'missing'}")
    if implementation_mode not in IMPLEMENTATION_MODES:
        reasons.append("invalid_implementation_mode")
    frontier_required = _frontier_required(payload, category, implementation_mode, preflight=preflight, cfg=cfg)
    if frontier_required and not _frontier_requirement_satisfied(payload, patch_generation, cfg):
        reasons.append("required_frontier_model_missing")
    if frontier_required and not (_frontier_reason(payload) or _frontier_model_called(patch_generation)):
        reasons.append("missing_frontier_escalation_reason")
    if not _proposal_evidence(payload):
        reasons.append("missing_evidence")
    has_patch_text = bool(diff_text.strip())
    if not has_patch_text:
        reasons.append("missing_unified_diff")
    if has_patch_text and not changed_files and not unavailable_reason:
        reasons.append("invalid_patch_format")
    if not changed_files and not unavailable_reason and "invalid_patch_format" not in reasons:
        reasons.append("no_changed_files")
    if unavailable_reason:
        reasons.append(f"patch_generation_unavailable:{unavailable_reason}")
    if preflight:
        scorecard = preflight.get("quality_scorecard") or {}
        if preflight.get("invalid_targets"):
            reasons.append("invalid_target_files")
        if not preflight.get("implementation_mode_valid", True):
            reasons.append("invalid_implementation_mode")
        if cfg.get("validate_test_commands", True) and preflight.get("test_issues"):
            reasons.append("invalid_test_commands")
        if scorecard.get("runtime_integration_status") in {"missing", "integration_claim_without_target"}:
            reasons.append("no_runtime_integration_target")
        if scorecard.get("reject_before_model_call"):
            status = scorecard.get("preflight_reject_status")
            if status:
                reasons.append(str(status))

    actual_runtime_status = _actual_runtime_integration_status(payload, category, changed_files) if changed_files else None
    if (
        cfg.get("reject_orphan_helpers", True)
        and actual_runtime_status == "changed_source_without_runtime_wiring"
    ):
        reasons.append("no_runtime_integration_target")

    for path in changed_files:
        if _path_blocked(path, cfg):
            reasons.append(f"path_not_allowed:{path}")

    reasons.extend(_forbidden_reasons_from_diff(diff_text))

    reasons = sorted(set(reasons))
    decision = _safety_decision(reasons, patch_generation=patch_generation, preflight=preflight)
    return {
        "allowed": not reasons,
        "decision": decision,
        "reasons": reasons,
        "category": category,
        "implementation_mode": implementation_mode,
        "frontier_required": frontier_required,
        "priority": priority,
        "changed_files": changed_files,
        "preflight": preflight or {},
        "proposal_quality_score": (preflight or {}).get("quality_scorecard", {}).get("proposal_quality_score"),
        "proposal_scorecard": (preflight or {}).get("quality_scorecard", {}),
        "actual_runtime_integration_status": actual_runtime_status,
        "frontier_call_useful": None,
        "frontier_call_wasted_reason": _frontier_wasted_reason(decision, reasons, patch_generation),
        "scanned_at": _utc_now(),
    }


def _frontier_model_called(patch_generation: dict | None) -> bool:
    if not patch_generation:
        return False
    return (
        str(patch_generation.get("requested_model_tier") or "").lower() == "frontier"
        or str(patch_generation.get("model_tier") or "").lower() == "frontier"
        or str(patch_generation.get("model_name") or "").lower().startswith("openai/gpt-5.")
    )


def _frontier_requirement_satisfied(payload: dict, patch_generation: dict | None, cfg: dict) -> bool:
    required_model = str(cfg.get("required_model", "openai/gpt-5.6-sol"))
    if _model_name(payload) == required_model:
        return True
    if not patch_generation:
        return False
    return (
        patch_generation.get("model_name") == required_model
        or str(patch_generation.get("requested_model_tier") or "").lower() == "frontier"
        or str(patch_generation.get("model_tier") or "").lower() == "frontier"
    )


def _frontier_wasted_reason(decision: str, reasons: list[str], patch_generation: dict | None) -> str | None:
    if not _frontier_model_called(patch_generation):
        return None
    if decision in {"auto_allowed", *SUCCESS_STATUSES}:
        return None
    text = json.dumps(patch_generation or {}, sort_keys=True).lower()
    if _patch_generation_unavailable_reason(patch_generation):
        return None
    if "insufficient_quota" in text or "429" in text:
        return "model_quota"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    for reason in (
        "no_changed_files",
        "invalid_patch_format",
        "invalid_test_commands",
        "missing_unified_diff",
        "no_runtime_integration_target",
        "invalid_target_files",
    ):
        if reason in reasons:
            return reason
    return decision


def _with_frontier_usefulness(safety: dict, status: str, patch_generation: dict | None) -> dict:
    output = dict(safety)
    if not _frontier_model_called(patch_generation):
        output["frontier_call_useful"] = None
        output["frontier_call_wasted_reason"] = None
        return output
    useful = status in SUCCESS_STATUSES and bool(output.get("changed_files"))
    output["frontier_call_useful"] = useful
    output["frontier_call_wasted_reason"] = None if useful else _frontier_wasted_reason(
        status,
        list(output.get("reasons") or []),
        patch_generation,
    )
    return output


def _frontier_waste_guard(conn: Any, cfg: dict, preflight: dict) -> dict | None:
    if not cfg.get("frontier_waste_guard_enabled", True):
        return None
    scorecard = preflight.get("quality_scorecard") or {}
    score = float(scorecard.get("proposal_quality_score") or 0.0)
    if score >= float(cfg.get("frontier_waste_guard_min_quality", 80)):
        return None
    recent = code_evolution_recent(conn, limit=int(cfg.get("frontier_waste_guard_recent", 12)))
    wasted = 0
    for row in recent:
        safety = row.get("safety") or {}
        patch_generation = safety.get("patch_generation") or {}
        if not _frontier_model_called(patch_generation):
            continue
        if row.get("status") in SUCCESS_STATUSES:
            continue
        if safety.get("frontier_call_wasted_reason") or row.get("status") in {
            "blocked_model_quota",
            "no_changed_files",
            "invalid_test_commands",
            "patch_generation_failed",
            "discarded_test_failure",
        }:
            wasted += 1
    threshold = int(cfg.get("frontier_waste_guard_after", 5))
    if wasted < threshold:
        return None
    return {
        "status": "rejected_frontier_waste_guard",
        "reason": "frontier_recent_waste_requires_repaired_preflight",
        "recent_frontier_waste_count": wasted,
        "proposal_quality_score": score,
        "min_quality_required": float(cfg.get("frontier_waste_guard_min_quality", 80)),
    }


def _duplicate_failure_guard(conn: Any, payload: dict, preflight: dict, cfg: dict) -> dict | None:
    if not cfg.get("duplicate_failure_suppression_enabled", True):
        return None
    current = _proposal_fingerprint(payload, preflight)
    if not current:
        return None
    threshold = int(cfg.get("duplicate_failure_suppression_after", 4))
    failures = 0
    examples: list[str] = []
    for row in code_evolution_recent(conn, limit=int(cfg.get("duplicate_failure_suppression_recent", 80))):
        status = str(row.get("status") or "")
        if status in {*SUCCESS_STATUSES, "queued_probation_limit"}:
            continue
        safety = row.get("safety") or {}
        if _patch_generation_unavailable_reason(safety.get("patch_generation") or {}):
            continue
        prior = _proposal_fingerprint(row.get("payload") or {}, safety.get("preflight") or {})
        if prior != current:
            continue
        if status in {
            "discarded_test_failure",
            "invalid_patch_format",
            "invalid_test_commands",
            "rejected_preflight_invalid_target",
            "rejected_preflight_invalid_tests",
            "rejected_preflight_no_runtime_integration",
            "patch_generation_failed",
            "no_changed_files",
            "blocked_safety",
        }:
            failures += 1
            examples.append(str(row.get("proposal_id") or ""))
    if failures < threshold:
        return None
    return {
        "status": "rejected_duplicate_recent_failure",
        "reason": "duplicate_recent_structural_failure",
        "fingerprint": current,
        "recent_failure_count": failures,
        "examples": examples[:5],
    }


def _proposal_fingerprint(payload: dict, preflight: dict | None = None) -> str:
    category = _normalize_category(_field(payload, "change_category", "category"))
    title = _normalize_proposal_text(str(payload.get("title") or ""))
    change = _normalize_proposal_text(str(_field(payload, "proposed_change", "rationale") or ""))[:120]
    targets = tuple(sorted((preflight or {}).get("target_files") or [])[:4])
    raw = json.dumps({"category": category, "title": title, "change": change, "targets": targets}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _normalize_proposal_text(value: str) -> str:
    text = value.lower()
    text = re.sub(r"\b(okx|bybit|kucoin|gate|mexc|kraken|coinbase|bitso|luno|valr)\b", "<venue>", text)
    text = re.sub(r"\b(gpt|frontier|standard|mini)\b", "", text)
    text = re.sub(r"[^a-z0-9_<>]+", " ", text)
    return " ".join(text.split())


def _path_blocked(path: str, cfg: dict) -> bool:
    for prefix in BLOCKED_PATH_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    allowed = cfg.get("allowed_path_prefixes", DEFAULT_ALLOWED_PATH_PREFIXES)
    return not any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in allowed)


def _command_display(command: list[str]) -> str:
    return " ".join(str(part) for part in command)


def _builder_context_metadata(context: dict) -> dict:
    return {
        "version": context.get("version"),
        "likely_tests": context.get("likely_tests", []),
        "files": [
            {
                "path": entry.get("path"),
                "exists": entry.get("exists"),
                "sha256": entry.get("sha256"),
                "symbols": entry.get("symbols", [])[:20],
                "truncated": entry.get("truncated", False),
            }
            for entry in context.get("files", [])
        ],
    }


def generate_patch_with_frontier_model(
    payload: dict,
    settings: dict,
    root: pathlib.Path = ROOT,
    preflight: dict | None = None,
) -> tuple[str, dict]:
    cfg = _cfg(settings)
    preflight = preflight or preflight_proposal(payload, settings, root=root)
    safe_files = [
        rel
        for rel in preflight.get("target_files", [])
        if rel and not _path_blocked(rel, cfg)
    ][:8]
    if not safe_files:
        category = _normalize_category(_field(payload, "change_category", "category"))
        safe_files = [
            rel
            for rel in DEFAULT_CATEGORY_FILES.get(category, [])
            if not _path_blocked(rel, cfg) and (root / rel).exists()
        ][:8]
    if not safe_files:
        return "", {"status": "skipped", "reason": "no_safe_expected_files"}

    patch_tier = _patch_generation_tier(payload, settings, preflight=preflight)
    reasoning_effort = _patch_reasoning_effort(patch_tier)
    max_chars = int(cfg.get("patch_generation_max_file_chars", 12000))
    builder_context = build_builder_context(
        root,
        safe_files,
        max_chars=max_chars,
        likely_tests=[_command_display(command) for command in _test_commands(payload, {**cfg, "run_full_regression": False}, root=root)],
    )
    rendered_context = render_builder_context(builder_context)

    system = (
        "You are the Build Planner for a paper-only trading research system. "
        "Return only a unified diff. Do not add live trading, credentials, broker writes, "
        "startup changes, destructive data actions, or raw installer commands. "
        "Python dependencies may be declared only in requirements-autonomous.txt or requirements-llm.txt."
    )
    prompt = (
        "Create a useful safe unified diff for this code-evolution proposal.\n"
        "Allowed paths are src/, tests/, config/, docs/, README.md, COST_AWARE_SWARM.md, "
        "LLM_AGENT_BRIDGE.md, requirements-autonomous.txt, and requirements-llm.txt.\n"
        "The patch must be paper-only and include or preserve tests where practical.\n\n"
        "Patch discipline:\n"
        "- prefer one focused, working change over a broad rewrite;\n"
        "- wiring existing helper code into the normal paper runner/report/LLM packet is allowed;\n"
        "- fixing or improving prior generated paper-only code is allowed;\n"
        "- feature-flag paper-only scoring, policy, variant, and adapter changes when behavior could affect trades;\n"
        "- use exact current file context, hashes, and symbols from BUILDER_CONTEXT;\n"
        "- return a valid `git apply` compatible unified diff with exact hunk headers;\n"
        "- do not use placeholder hunks like `@@?`, ellipses, omitted context, or prose outside the diff;\n"
        "- include complete added files and complete modified hunks; no truncated patches;\n"
        "- do not wrap or duplicate existing functions when a local helper or report field is enough;\n"
        "- if the proposal is too large, implement the highest-value safe part that is actually used by runtime code.\n\n"
        f"PREFLIGHT:\n{json.dumps(preflight, sort_keys=True)}\n\n"
        f"PROPOSAL:\n{json.dumps(payload, sort_keys=True)}\n\n"
        f"{rendered_context}"
    )
    preflight_status = completion_preflight_status(
        "build_planner",
        prompt,
        system=system,
        tier_override=patch_tier,
    )
    if not preflight_status.get("ok"):
        return "", {
            "status": preflight_status.get("status"),
            "model_name": preflight_status.get("model_name"),
            "model_tier": preflight_status.get("model_tier"),
            "requested_model_tier": patch_tier,
            "implementation_mode": preflight.get("implementation_mode"),
            "estimated_cost_usd": 0.0,
            "prompt_tokens": preflight_status.get("prompt_tokens", 0),
            "completion_tokens": 0,
            "builder_context": _builder_context_metadata(builder_context),
            "returned_patch_format": "unavailable_before_model_call",
            "preflight_skipped_model_call": True,
        }
    result = complete(
        "build_planner",
        prompt,
        system=system,
        tier_override=patch_tier,
        operation="code_evolution_patch_generation",
        frontier_escalation_reason=(
            _frontier_reason(payload) or "Code evolution patch generation."
            if patch_tier == "frontier"
            else None
        ),
        reasoning_effort_override=reasoning_effort,
        structured_json=False,
        max_output_tokens_override=int(cfg.get("patch_generation_max_output_tokens", 16000)),
        timeout_seconds_override=float(cfg.get("patch_generation_timeout_seconds", 90)),
    )
    generated_text = _strip_fence(result.text)
    if not result.status.startswith("model_call:"):
        generated_text = ""
    return generated_text, {
        "status": result.status,
        "model_name": result.model_name,
        "model_tier": result.model_tier,
        "requested_model_tier": patch_tier,
        "implementation_mode": preflight.get("implementation_mode"),
        "estimated_cost_usd": result.estimated_cost_usd,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "builder_context": _builder_context_metadata(builder_context),
        "returned_patch_format": "unified_diff" if _looks_like_unified_diff(generated_text) else "invalid_or_empty",
    }


def repair_patch_with_frontier_model(
    payload: dict,
    settings: dict,
    diff_text: str,
    failure: dict,
    root: pathlib.Path = ROOT,
) -> tuple[str, dict]:
    cfg = _cfg(settings)
    changed_files = changed_files_from_diff(diff_text)
    safe_files = [
        rel
        for rel in changed_files
        if rel and not _path_blocked(rel, cfg)
    ][:8]
    if not safe_files:
        category = _normalize_category(_field(payload, "change_category", "category"))
        safe_files = [
            rel
            for rel in DEFAULT_CATEGORY_FILES.get(category, [])
            if not _path_blocked(rel, cfg) and (root / rel).exists()
        ][:8]
    if not safe_files:
        return "", {"status": "skipped", "reason": "no_safe_files_for_repair"}

    max_chars = int(cfg.get("patch_generation_max_file_chars", 12000))
    builder_context = build_builder_context(
        root,
        safe_files,
        max_chars=max_chars,
        likely_tests=[_command_display(command) for command in _test_commands(payload, {**cfg, "run_full_regression": False}, root=root)],
    )
    rendered_context = render_builder_context(builder_context)

    commands = failure.get("commands") if isinstance(failure, dict) else []
    error_tail = ""
    if commands and isinstance(commands[-1], dict):
        error_tail = str(commands[-1].get("stderr_tail") or commands[-1].get("stdout_tail") or "")[-4000:]
    failure_stage = str(failure.get("stage") or "unknown") if isinstance(failure, dict) else "unknown"
    system = (
        "You repair unified diffs for a paper-only trading research system. "
        "Return only a corrected unified diff. Do not add live trading, credentials, "
        "broker writes, startup changes, destructive data actions, or raw installer commands. "
        "Python dependencies may be declared only in requirements-autonomous.txt or requirements-llm.txt."
    )
    prompt = (
        f"The previous safe-scope patch failed at sandbox stage `{failure_stage}`. "
        "Return a corrected full unified diff only, using the exact current BUILDER_CONTEXT. "
        "Keep the same intended paper-only behavior and touch only the same safe files. "
        "Use valid `git apply` compatible hunk headers, no `@@?` placeholders, no ellipses, "
        "and no prose outside the diff. If tests failed, fix the patch so the requested "
        "safe tests pass without weakening safety rules.\n\n"
        f"APPLY_ERROR:\n{error_tail}\n\n"
        f"PROPOSAL:\n{json.dumps(payload, sort_keys=True)}\n\n"
        f"FAILED_DIFF:\n{diff_text[:20000]}\n\n"
        f"{rendered_context}"
    )
    category = _normalize_category(_field(payload, "change_category", "category"))
    repair_tier = str(cfg.get("repair_patch_tier", cfg.get("standard_patch_tier", "fast")))
    result = complete(
        "build_planner",
        prompt,
        system=system,
        tier_override=repair_tier,
        operation="code_evolution_patch_repair",
        frontier_escalation_reason=(
            _frontier_reason(payload) or "Code evolution patch repair requires frontier reasoning."
            if repair_tier == "frontier"
            else None
        ),
        reasoning_effort_override=_patch_reasoning_effort(repair_tier),
        structured_json=False,
        max_output_tokens_override=int(cfg.get("patch_generation_max_output_tokens", 16000)),
        timeout_seconds_override=float(cfg.get("patch_generation_timeout_seconds", 90)),
    )
    repaired_text = _strip_fence(result.text)
    if not result.status.startswith("model_call:"):
        repaired_text = ""
    return repaired_text, {
        "status": result.status,
        "model_name": result.model_name,
        "model_tier": result.model_tier,
        "requested_model_tier": repair_tier,
        "implementation_mode": _implementation_mode(payload, category),
        "estimated_cost_usd": result.estimated_cost_usd,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "builder_context": _builder_context_metadata(builder_context),
        "returned_patch_format": "unified_diff" if _looks_like_unified_diff(repaired_text) else "invalid_or_empty",
    }


def _repair_invalid_patch_format_once(
    payload: dict,
    settings: dict,
    diff_text: str,
    safety: dict,
    patch_generation: dict,
    preflight: dict,
    root: pathlib.Path = ROOT,
) -> tuple[str, dict, dict, list[dict[str, Any]]]:
    if not diff_text.strip():
        return diff_text, safety, patch_generation, []
    if safety.get("decision") != "invalid_patch_format":
        return diff_text, safety, patch_generation, []
    if not _cfg(settings).get("repair_invalid_patch_format", True):
        return diff_text, safety, patch_generation, []

    failure = {
        "passed": False,
        "stage": "invalid_patch_format",
        "error": "model output was not a git-applyable unified diff",
        "commands": [
            {
                "returncode": 1,
                "stderr_tail": "Patch generation returned text, but no changed files could be parsed. Convert the intended change into a valid unified diff only.",
            }
        ],
    }
    repaired_diff, repair_generation = repair_patch_with_frontier_model(
        payload,
        settings,
        diff_text,
        failure,
        root=root,
    )
    repaired_diff = rewrite_diff_paths(repaired_diff)
    repair_generation = {**repair_generation, "attempt": 1, "repair_reason": "invalid_patch_format"}
    repair_entry: dict[str, Any] = {
        "attempt": 1,
        "previous_stage": "invalid_patch_format",
        "generation": repair_generation,
    }
    if not repaired_diff.strip():
        repair_entry["result"] = "empty_repair"
        return diff_text, safety, patch_generation, [repair_entry]

    repaired_safety = validate_and_scan(
        payload,
        repaired_diff,
        settings,
        patch_generation=repair_generation,
        preflight=preflight,
    )
    repair_entry["safety"] = repaired_safety
    repair_entry["result"] = "passed" if repaired_safety.get("allowed") else "blocked_by_safety"
    combined_generation = {
        **patch_generation,
        "invalid_patch_format_repair": repair_generation,
        "returned_patch_format": repair_generation.get("returned_patch_format"),
    }
    if repaired_safety.get("allowed"):
        return repaired_diff, repaired_safety, combined_generation, [repair_entry]
    return repaired_diff, repaired_safety, combined_generation, [repair_entry]


def run_sandbox_checks(diff_text: str, payload: dict, settings: dict, root: pathlib.Path = ROOT) -> dict:
    diff_text = rewrite_diff_paths(diff_text)
    cfg = _cfg(settings)
    timeout = int(cfg.get("sandbox_timeout_seconds", 120))
    with tempfile.TemporaryDirectory(prefix="radar_code_evolution_") as tmp:
        sandbox = pathlib.Path(tmp) / "workspace"
        _copy_workspace(root, sandbox)
        patch_path = pathlib.Path(tmp) / "change.patch"
        patch_path.write_text(diff_text, encoding="utf-8")

        check = _run(["git", "apply", "--check", "--recount", str(patch_path)], sandbox, timeout)
        if check["returncode"] == 0:
            apply = _run(["git", "apply", "--recount", str(patch_path)], sandbox, timeout)
            if apply["returncode"] != 0:
                return {"passed": False, "stage": "patch_apply", "commands": [check, apply]}
            materialized = _materialize_added_files_if_missing(diff_text, sandbox)
            commands = [check, apply]
        else:
            internal = _internal_apply_command(
                diff_text,
                sandbox,
                enabled=bool(cfg.get("local_context_patch_apply", True)),
            )
            if internal["returncode"] != 0:
                return {"passed": False, "stage": "patch_check", "commands": [check, internal]}
            materialized = []
            commands = [check, internal]

        dependency_install = _install_dependency_manifests_for_diff(
            diff_text,
            sandbox,
            timeout=timeout,
            enabled=bool(cfg.get("install_changed_python_dependencies", True)),
        )
        commands.extend(dependency_install)
        for result in dependency_install:
            if result["returncode"] != 0:
                return {"passed": False, "stage": "dependency_install", "commands": commands}

        for command in _test_commands(payload, cfg, root=sandbox):
            result = _run(command, sandbox, timeout)
            commands.append(result)
            if result["returncode"] != 0:
                return {"passed": False, "stage": "tests", "commands": commands}
        return {"passed": True, "stage": "passed", "commands": commands, "materialized_added_files": materialized}


def _copy_workspace(root: pathlib.Path, sandbox: pathlib.Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".venv", "runs", "__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(root, sandbox, ignore=ignore)


def _dependency_manifests_from_diff(diff_text: str) -> list[str]:
    return [
        path
        for path in changed_files_from_diff(diff_text)
        if path in DEPENDENCY_MANIFESTS
    ]


def _install_dependency_manifests_for_diff(
    diff_text: str,
    root: pathlib.Path,
    *,
    timeout: int,
    enabled: bool,
) -> list[dict]:
    if not enabled:
        return []
    commands = []
    for rel in _dependency_manifests_from_diff(diff_text):
        manifest = root / rel
        if not manifest.exists():
            continue
        commands.append(
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    rel,
                ],
                root,
                timeout,
            )
        )
    return commands


def _test_commands(payload: dict, cfg: dict, root: pathlib.Path = ROOT) -> list[list[str]]:
    commands: list[list[str]] = []
    for parsed in payload.get("_preflight_parsed_tests") or []:
        if isinstance(parsed, list) and parsed and parsed not in commands:
            commands.append([str(part) for part in parsed])
    raw_tests = _field(payload, "tests_to_run", "tests") or []
    if isinstance(raw_tests, str):
        raw_tests = [raw_tests]
    if isinstance(raw_tests, list):
        for item in raw_tests[:3]:
            parsed = _safe_test_command(str(item), root=root)
            if parsed and parsed not in commands:
                commands.append(parsed)
    if cfg.get("run_full_regression", True):
        full = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
        if full not in commands:
            commands.append(full)
    return commands


def _safe_test_command(command: str, root: pathlib.Path = ROOT) -> list[str] | None:
    parsed = _safe_compileall_command(command, root=root)
    if parsed:
        return parsed
    parsed = _safe_unittest_command(command, root=root)
    if parsed:
        return parsed
    return _safe_pytest_path_as_unittest(command, root=root)


def _strip_shell_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _safe_compileall_command(command: str, root: pathlib.Path = ROOT) -> list[str] | None:
    cleaned = command.strip().replace("\\", "/").lower()
    forbidden = [";", "&", "|", ">", "<", "`", "$("]
    if any(token in cleaned for token in forbidden):
        return None
    accepted = {
        "python -m compileall .",
        "python -m compileall src",
        "python -m compileall src tests",
        f"{pathlib.Path(sys.executable).name.lower()} -m compileall .",
    }
    if cleaned not in accepted:
        return None
    targets = ["src"]
    if " tests" in cleaned:
        targets.append("tests")
    return [sys.executable, "-m", "compileall", *targets]


def _safe_unittest_command(command: str, root: pathlib.Path = ROOT) -> list[str] | None:
    cleaned = command.strip().replace("\\", "/")
    forbidden = [";", "&", "|", ">", "<", "`", "$("]
    if any(token in cleaned for token in forbidden):
        return None
    prefixes = ("python -m unittest", "python.exe -m unittest", f"{pathlib.Path(sys.executable).name} -m unittest")
    if not cleaned.lower().startswith(prefixes):
        return None
    parts = [_strip_shell_quotes(part) for part in cleaned.split()]
    try:
        idx = parts.index("unittest")
    except ValueError:
        return None
    tail = []
    for part in parts[idx + 1 :]:
        part = _strip_shell_quotes(part)
        path_part = part.split("::", 1)[0]
        norm = _canonical_path(path_part)
        if norm.startswith("tests/") and norm.endswith(".py"):
            if not (root / norm).exists():
                return None
            tail.append(norm)
        else:
            tail.append(part)
    return [sys.executable, "-m", "unittest", *tail]


def _safe_pytest_path_as_unittest(command: str, root: pathlib.Path = ROOT) -> list[str] | None:
    cleaned = command.strip().replace("\\", "/")
    forbidden = [";", "&", "|", ">", "<", "`", "$("]
    if any(token in cleaned for token in forbidden):
        return None
    parts = [_strip_shell_quotes(part) for part in cleaned.split()]
    if not parts or parts[0].lower() not in {"pytest", "py.test", "python"}:
        return None
    if parts[0].lower() == "python":
        if len(parts) < 3 or parts[1:3] != ["-m", "pytest"]:
            return None
        parts = ["pytest", *parts[3:]]
    safe_paths = []
    allowed_flags = {"-q", "-v", "-x"}
    for part in parts[1:]:
        part = _strip_shell_quotes(part)
        if part in allowed_flags:
            continue
        path_part = part.split("::", 1)[0]
        if not path_part:
            continue
        norm = _normalize_path(path_part)
        norm = _canonical_path(norm)
        if not norm or not norm.startswith("tests/") or not norm.endswith(".py"):
            return None
        if not (root / norm).exists():
            return None
        safe_paths.append(norm)
    if not safe_paths:
        return None
    return [sys.executable, "-m", "unittest", *safe_paths[:3]]


def _run(args: list[str], cwd: pathlib.Path, timeout: int) -> dict:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        return {
            "args": args,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
    except Exception as exc:  # noqa: BLE001
        return {"args": args, "returncode": 999, "stdout_tail": "", "stderr_tail": str(exc)}


_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


def _diff_file_path(raw: str) -> str:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return ""
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return _canonical_path(path)


def _parse_unified_diff(diff_text: str) -> tuple[list[dict[str, Any]], str | None]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish_current() -> None:
        if current and current.get("path") and current.get("hunks"):
            files.append(current)

    for line_number, raw in enumerate(diff_text.splitlines(), 1):
        line = raw.rstrip("\r")
        if line.startswith("diff --git "):
            finish_current()
            parts = line.split()
            path = _diff_file_path(parts[3]) if len(parts) >= 4 else ""
            current = {"path": path, "hunks": [], "new_file": False, "delete_file": False}
            continue
        if current is None:
            continue
        if line.startswith("--- "):
            old_path = _diff_file_path(line[4:])
            current["new_file"] = not old_path
            continue
        if line.startswith("+++ "):
            new_path = _diff_file_path(line[4:])
            current["delete_file"] = not new_path
            if new_path:
                current["path"] = new_path
            continue
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            current["hunks"].append(
                {
                    "old_start": int(hunk_match.group(1)),
                    "new_start": int(hunk_match.group(2)),
                    "lines": [],
                }
            )
            continue
        if line.startswith(("index ", "new file ", "deleted file ", "similarity ", "rename ", "old mode ", "new mode ")):
            continue
        if line.startswith("\\ No newline"):
            continue
        if current.get("hunks") and line[:1] in {" ", "+", "-"}:
            current["hunks"][-1]["lines"].append((line[0], line[1:]))
            continue
        if current.get("hunks") and line.strip():
            return [], f"unsupported_hunk_line:{line_number}:{line[:80]}"
    finish_current()
    return files, None


def _find_hunk_position(lines: list[str], old_lines: list[str], preferred: int) -> int | None:
    if not old_lines:
        return max(0, min(preferred, len(lines)))
    width = len(old_lines)
    if width > len(lines):
        return None

    def matches_at(index: int) -> bool:
        return lines[index : index + width] == old_lines

    start = max(0, min(preferred, len(lines) - width))
    near_min = max(0, start - 250)
    near_max = min(len(lines) - width, start + 250)
    near_matches = [idx for idx in range(near_min, near_max + 1) if matches_at(idx)]
    if near_matches:
        return min(near_matches, key=lambda idx: abs(idx - start))

    all_matches = [idx for idx in range(0, len(lines) - width + 1) if matches_at(idx)]
    if all_matches:
        return min(all_matches, key=lambda idx: abs(idx - start))
    return None


def _safe_child_path(root: pathlib.Path, rel: str) -> pathlib.Path | None:
    if not rel or _path_blocked(rel, _cfg({})):
        return None
    root_resolved = root.resolve()
    path = (root_resolved / rel).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError:
        return None
    return path


def _apply_unified_diff_by_context(diff_text: str, root: pathlib.Path) -> dict:
    files, parse_error = _parse_unified_diff(diff_text)
    if parse_error:
        return {"applied": False, "stage": "internal_parse", "error": parse_error, "changed_files": []}
    if not files:
        return {"applied": False, "stage": "internal_parse", "error": "no_parseable_files", "changed_files": []}

    writes: list[tuple[pathlib.Path, str, str]] = []
    changed_files: list[str] = []
    for file_patch in files:
        rel = str(file_patch.get("path") or "")
        path = _safe_child_path(root, rel)
        if path is None:
            return {"applied": False, "stage": "internal_path_check", "error": f"path_not_allowed:{rel}", "changed_files": changed_files}
        if file_patch.get("delete_file"):
            return {"applied": False, "stage": "internal_parse", "error": f"delete_not_allowed:{rel}", "changed_files": changed_files}

        original = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        original_had_newline = original.endswith("\n")
        lines = original.splitlines()
        for hunk_index, hunk in enumerate(file_patch.get("hunks", []), 1):
            old_lines = [text for kind, text in hunk["lines"] if kind in {" ", "-"}]
            new_lines = [text for kind, text in hunk["lines"] if kind in {" ", "+"}]
            preferred = max(0, int(hunk.get("old_start") or 1) - 1)
            position = _find_hunk_position(lines, old_lines, preferred)
            if position is None:
                return {
                    "applied": False,
                    "stage": "internal_context_match",
                    "error": f"hunk_not_matched:{rel}:{hunk_index}",
                    "changed_files": changed_files,
                }
            lines = lines[:position] + new_lines + lines[position + len(old_lines) :]

        new_text = "\n".join(lines)
        if lines and (original_had_newline or file_patch.get("new_file", False)):
            new_text += "\n"
        writes.append((path, rel, new_text))
        changed_files.append(rel)

    for path, _rel, new_text in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    return {"applied": True, "stage": "internal_context_apply", "changed_files": changed_files}


def _internal_apply_command(diff_text: str, root: pathlib.Path, enabled: bool) -> dict:
    if not enabled:
        return {
            "args": ["internal", "context_apply"],
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "local_context_patch_apply_disabled",
        }
    result = _apply_unified_diff_by_context(diff_text, root)
    return {
        "args": ["internal", "context_apply"],
        "returncode": 0 if result.get("applied") else 1,
        "stdout_tail": json.dumps({k: v for k, v in result.items() if k != "error"})[-2000:],
        "stderr_tail": str(result.get("error") or "")[-2000:],
        "internal_result": result,
    }


def apply_patch_to_workspace(
    diff_text: str,
    root: pathlib.Path = ROOT,
    timeout: int = 120,
    local_context_patch_apply: bool = True,
) -> dict:
    diff_text = rewrite_diff_paths(diff_text)
    apply_root, apply_diff, apply_prefix = _patch_apply_location(root, diff_text, timeout)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".patch") as fh:
        fh.write(apply_diff)
        patch_name = fh.name
    try:
        check = _run(["git", "apply", "--check", "--recount", patch_name], apply_root, timeout)
        if check["returncode"] == 0:
            apply = _run(["git", "apply", "--recount", patch_name], apply_root, timeout)
            materialized = _materialize_added_files_if_missing(diff_text, root) if apply["returncode"] == 0 else []
            return {
                "applied": apply["returncode"] == 0,
                "stage": "patch_apply",
                "apply_root": str(apply_root),
                "app_path_prefix": apply_prefix,
                "commands": [check, apply],
                "materialized_added_files": materialized,
            }
        internal = _internal_apply_command(diff_text, root, enabled=local_context_patch_apply)
        return {
            "applied": internal["returncode"] == 0,
            "stage": "internal_context_apply" if internal["returncode"] == 0 else "patch_check",
            "apply_root": str(root),
            "app_path_prefix": None,
            "commands": [check, internal],
            "materialized_added_files": [],
        }
    finally:
        try:
            os.unlink(patch_name)
        except OSError:
            pass


def reverse_patch_in_workspace(diff_text: str, root: pathlib.Path = ROOT, timeout: int = 120) -> dict:
    diff_text = rewrite_diff_paths(diff_text)
    apply_root, apply_diff, apply_prefix = _patch_apply_location(root, diff_text, timeout)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".patch") as fh:
        fh.write(apply_diff)
        patch_name = fh.name
    try:
        check = _run(["git", "apply", "-R", "--check", "--recount", patch_name], apply_root, timeout)
        if check["returncode"] != 0:
            return {
                "reverted": False,
                "stage": "reverse_check",
                "apply_root": str(apply_root),
                "app_path_prefix": apply_prefix,
                "commands": [check],
            }
        apply = _run(["git", "apply", "-R", "--recount", patch_name], apply_root, timeout)
        return {
            "reverted": apply["returncode"] == 0,
            "stage": "reverse_apply",
            "apply_root": str(apply_root),
            "app_path_prefix": apply_prefix,
            "commands": [check, apply],
        }
    finally:
        try:
            os.unlink(patch_name)
        except OSError:
            pass


def _materialize_added_files_if_missing(diff_text: str, root: pathlib.Path) -> list[str]:
    materialized: list[str] = []
    for rel, content in _added_files_from_diff(diff_text).items():
        if _path_blocked(rel, _cfg({})):
            continue
        path = root / rel
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        materialized.append(rel)
    return materialized


def _added_files_from_diff(diff_text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    current: str | None = None
    is_new_file = False
    in_hunk = False
    lines: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            if current and is_new_file:
                files[current] = "\n".join(lines) + "\n"
            current = None
            is_new_file = False
            in_hunk = False
            lines = []
            parts = raw.split()
            if len(parts) >= 4:
                current = _canonical_path(parts[3][2:] if parts[3].startswith("b/") else parts[3])
            continue
        if current is None:
            continue
        if raw.startswith("--- /dev/null"):
            is_new_file = True
            continue
        if raw.startswith("@@ "):
            in_hunk = True
            continue
        if not in_hunk or not is_new_file:
            continue
        if raw.startswith("+") and not raw.startswith("+++ "):
            lines.append(raw[1:])
        elif raw.startswith(" "):
            lines.append(raw[1:])
    if current and is_new_file:
        files[current] = "\n".join(lines) + "\n"
    return files


def _run_candidate_tests(payload: dict, cfg: dict, app_root: pathlib.Path) -> dict:
    commands = []
    timeout = int(cfg.get("sandbox_timeout_seconds", 120))
    for command in _test_commands(payload, cfg, root=app_root):
        result = _run(command, app_root, timeout)
        commands.append(result)
        if result["returncode"] != 0:
            return {"passed": False, "stage": "tests", "commands": commands}
    return {"passed": True, "stage": "passed", "commands": commands}


def _run_git_release_pipeline(
    proposal_id: str,
    diff_text: str,
    payload: dict,
    settings: dict,
    safety: dict,
    sandbox: dict,
    root: pathlib.Path,
) -> dict:
    cfg = _cfg(settings)
    base_dir = pathlib.Path(str(cfg.get("release_worktree_dir") or RUNS_DIR / "evolution_worktrees"))
    base_dir.mkdir(parents=True, exist_ok=True)
    release, preflight = create_candidate_worktree(
        root,
        proposal_id,
        base_dir=base_dir,
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
    )
    if release is None:
        return {
            "status": "release_preflight_failed",
            "release": preflight,
            "tests": {"sandbox": sandbox},
            "evaluation": {"release": preflight},
        }

    app_root = pathlib.Path(release.app_worktree_path)

    def archive(status: str, **extra: Any) -> dict:
        metadata = {**release.as_metadata(), "preflight": preflight, "status": status, **extra}
        write_candidate_archive(RUNS_DIR / "candidate_archive.jsonl", metadata)
        return metadata

    apply_result = apply_patch_to_workspace(
        diff_text,
        root=app_root,
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
        local_context_patch_apply=bool(cfg.get("local_context_patch_apply", True)),
    )
    tests: dict[str, Any] = {"sandbox": sandbox, "candidate_apply": apply_result}
    if not apply_result.get("applied"):
        release.status = "implementation_failed"
        cleanup = cleanup_worktree(release, root)
        archived = archive("implementation_failed", cleanup=cleanup, reason="candidate_apply_failed")
        return {
            "status": "implementation_failed",
            "release": archived,
            "tests": tests,
            "evaluation": {"release": release.as_metadata(), "reason": "candidate_apply_failed"},
        }

    dependency_install = _install_dependency_manifests_for_diff(
        diff_text,
        app_root,
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
        enabled=bool(cfg.get("install_changed_python_dependencies", True)),
    )
    if dependency_install:
        tests["candidate_dependency_install"] = dependency_install
    if any(result["returncode"] != 0 for result in dependency_install):
        release.status = "implementation_failed"
        cleanup = cleanup_worktree(release, root)
        archived = archive("dependency_install_failed", cleanup=cleanup, reason="candidate_dependency_install_failed")
        return {
            "status": "dependency_install_failed",
            "release": archived,
            "tests": tests,
            "evaluation": {"release": release.as_metadata(), "reason": "candidate_dependency_install_failed"},
        }

    candidate_tests = _run_candidate_tests(payload, cfg, app_root)
    tests["candidate_tests"] = candidate_tests
    if not candidate_tests.get("passed"):
        status = classify_sandbox_failure(candidate_tests)
        release.status = "implementation_failed"
        cleanup = cleanup_worktree(release, root)
        archived = archive(status, cleanup=cleanup, reason="candidate_tests_failed")
        return {
            "status": status,
            "release": archived,
            "tests": tests,
            "evaluation": {"release": release.as_metadata(), "reason": "candidate_tests_failed"},
        }

    category = str(safety.get("category") or "")
    if category == "evolution_loop_improvement":
        before_benchmark = run_builder_failure_benchmark(root)
        after_benchmark = run_builder_failure_benchmark(app_root)
        benchmark_gate = benchmark_builder_change(
            {
                "before_solve_rate": before_benchmark.get("solve_rate"),
                "after_solve_rate": after_benchmark.get("solve_rate"),
            }
        )
        tests["builder_failure_benchmark"] = {
            "before": before_benchmark,
            "after": after_benchmark,
            "gate": benchmark_gate,
        }
        if not after_benchmark.get("passed") or not benchmark_gate.get("passed"):
            release.status = "archived_failed"
            cleanup = cleanup_worktree(release, root)
            archived = archive("archived_failed", cleanup=cleanup, reason="builder_failure_benchmark_failed")
            return {
                "status": "archived_failed",
                "release": archived,
                "tests": tests,
                "evaluation": {"release": release.as_metadata(), "reason": "builder_failure_benchmark_failed"},
            }

    release, commit_result = commit_candidate(
        release,
        f"Autonomous candidate {proposal_id}",
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
    )
    tests["candidate_commit"] = commit_result
    if not commit_result.get("ok"):
        release.status = "implementation_failed"
        cleanup = cleanup_worktree(release, root)
        archived = archive("implementation_failed", cleanup=cleanup, reason=commit_result.get("reason"))
        return {
            "status": "implementation_failed",
            "release": archived,
            "tests": tests,
            "evaluation": {"release": release.as_metadata(), "reason": commit_result.get("reason")},
        }

    canary = (
        run_radar_canary(
            app_root,
            timeout_seconds=int(cfg.get("canary_timeout_seconds", 180)),
            max_latency_seconds=float(cfg.get("canary_max_latency_seconds", 180.0)),
        )
        if cfg.get("run_candidate_canary", False)
        else skip_canary("candidate_canary_disabled")
    )
    release.canary = canary
    gate = evaluate_candidate(
        sandbox={"passed": True, "stage": "passed", "commands": tests.get("candidate_tests", {}).get("commands", [])},
        canary=canary,
        category=str(safety.get("category") or ""),
        changed_files=list(safety.get("changed_files") or []),
    )
    evaluation = {"release": release.as_metadata(), "gate": gate}
    if not gate.get("passed"):
        release.status = str(gate.get("status") or "archived_failed")
        cleanup = cleanup_worktree(release, root)
        archived = archive(release.status, cleanup=cleanup, reason=gate.get("reason"))
        return {
            "status": release.status,
            "release": archived,
            "tests": tests,
            "canary": canary,
            "evaluation": evaluation,
        }

    if cfg.get("promote_candidate_after_canary", False):
        release, promotion = promote_candidate(
            release,
            root,
            timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
        )
        evaluation["promotion"] = promotion
        if not promotion.get("ok"):
            release.status = "archived_failed"
            cleanup = cleanup_worktree(release, root)
            archived = archive("archived_failed", cleanup=cleanup, reason=promotion.get("reason"), promotion=promotion)
            return {
                "status": "archived_failed",
                "release": archived,
                "tests": tests,
                "canary": canary,
                "evaluation": evaluation,
            }
        release.promotion_reason = str(gate.get("reason") or "candidate passed sandbox gates")
        promotion["promotion_reason"] = release.promotion_reason
        status = "promoted"
    else:
        status = "candidate_committed"

    cleanup = cleanup_worktree(release, root)
    metadata = archive(status, cleanup=cleanup)
    return {
        "status": status,
        "release": metadata,
        "tests": tests,
        "canary": canary,
        "evaluation": evaluation,
    }


def process_code_change_recommendation(conn: Any, rec: dict, settings: dict, root: pathlib.Path = ROOT) -> list[dict]:
    payload = dict(rec.get("payload") or {})
    cfg = _cfg(settings)
    category = _normalize_category(_field(payload, "change_category", "category"))
    priority = int(payload.get("priority", rec.get("priority", 50)) or 50)
    title = str(payload.get("title") or rec.get("title") or "LLM code evolution proposal")[:180]
    evidence = _proposal_evidence(payload)
    proposal_id = _proposal_id(rec.get("recommendation_id"), payload)
    add_code_evolution_proposal(
        conn,
        proposal_id,
        rec.get("recommendation_id"),
        payload.get("agent_name"),
        _model_name(payload),
        _model_tier(payload),
        _frontier_reason(payload),
        title,
        category or "unknown",
        priority,
        payload,
        evidence,
    )
    preflight = preflight_proposal(payload, settings, root=root)
    if preflight.get("parsed_tests"):
        payload["_preflight_parsed_tests"] = preflight["parsed_tests"]
    payload.setdefault("implementation_mode", preflight.get("implementation_mode"))

    if not cfg.get("enabled", True):
        update_code_evolution_proposal(conn, proposal_id, status="disabled")
        _append_ledger(conn, proposal_id, "disabled")
        return [_artifact(proposal_id, "skipped", "disabled")]

    diff_text = rewrite_diff_paths(_extract_patch(payload))
    patch_generation = {}
    scorecard = preflight.get("quality_scorecard") or {}
    duplicate_guard = _duplicate_failure_guard(conn, payload, preflight, cfg)
    if duplicate_guard:
        safety = validate_and_scan(payload, diff_text, settings, preflight=preflight)
        safety["allowed"] = False
        safety["decision"] = duplicate_guard["status"]
        safety["reasons"] = sorted(set([*safety.get("reasons", []), duplicate_guard["reason"]]))
        safety["duplicate_failure_guard"] = duplicate_guard
        update_code_evolution_proposal(
            conn,
            proposal_id,
            patch_text=diff_text or None,
            changed_files=safety.get("changed_files", []),
            safety={**safety, "patch_generation": patch_generation},
            status=duplicate_guard["status"],
        )
        _append_ledger(conn, proposal_id, duplicate_guard["status"])
        return [_artifact(proposal_id, "created", duplicate_guard["status"], safety.get("reasons"))]

    if scorecard.get("reject_before_model_call"):
        safety = validate_and_scan(payload, diff_text, settings, preflight=preflight)
        update_code_evolution_proposal(
            conn,
            proposal_id,
            patch_text=diff_text or None,
            changed_files=safety.get("changed_files", []),
            safety={**safety, "patch_generation": patch_generation},
            status=safety["decision"],
        )
        _append_ledger(conn, proposal_id, safety["decision"])
        return [_artifact(proposal_id, "created", safety["decision"], safety.get("reasons"))]

    active = code_evolution_by_status(conn, sorted(PROBATION_STATUSES), limit=10)
    if len(active) >= int(cfg.get("max_auto_merges_per_loop", 1)):
        update_code_evolution_proposal(
            conn,
            proposal_id,
            status="queued_probation_limit",
            safety={"allowed": False, "reasons": ["active_probation_limit"]},
        )
        _append_ledger(conn, proposal_id, "queued_probation_limit")
        return [_artifact(proposal_id, "created", "queued_probation_limit")]

    if cfg.get("validate_test_commands", True) and preflight.get("test_issues") and not preflight.get("test_repairs"):
        safety = validate_and_scan(payload, diff_text, settings, preflight=preflight)
        if "invalid_test_commands" not in safety["reasons"]:
            safety["reasons"] = sorted(set([*safety["reasons"], "invalid_test_commands"]))
            safety["decision"] = _safety_decision(safety["reasons"], preflight=preflight)
            safety["allowed"] = False
        update_code_evolution_proposal(
            conn,
            proposal_id,
            patch_text=diff_text or None,
            changed_files=safety.get("changed_files", []),
            safety={**safety, "patch_generation": patch_generation},
            status=safety["decision"],
        )
        _append_ledger(conn, proposal_id, safety["decision"])
        return [_artifact(proposal_id, "created", safety["decision"], safety.get("reasons"))]

    if cfg.get("git_release_enabled", False):
        release_gate = release_preflight(root)
        if not release_gate.get("ok"):
            safety = validate_and_scan(payload, diff_text, settings, preflight=preflight)
            safety["allowed"] = False
            safety["decision"] = "release_preflight_failed"
            safety["release_preflight"] = release_gate
            update_code_evolution_proposal(
                conn,
                proposal_id,
                patch_text=diff_text or None,
                changed_files=safety.get("changed_files", []),
                safety={**safety, "patch_generation": patch_generation},
                status="release_preflight_failed",
                evaluation={"release": release_gate},
                parent_commit=release_gate.get("parent_commit"),
            )
            _append_ledger(conn, proposal_id, "release_preflight_failed")
            return [_artifact(proposal_id, "created", "release_preflight_failed", [str(release_gate.get("reason"))])]

    if not diff_text and cfg.get("generate_patch_when_missing", True):
        planned_tier = _patch_generation_tier(payload, settings, preflight=preflight)
        if planned_tier == "frontier":
            guard = _frontier_waste_guard(conn, cfg, preflight)
            if guard:
                safety = validate_and_scan(payload, diff_text, settings, preflight=preflight)
                safety["allowed"] = False
                safety["decision"] = guard["status"]
                safety["reasons"] = sorted(set([*safety.get("reasons", []), guard["reason"]]))
                safety["frontier_waste_guard"] = guard
                update_code_evolution_proposal(
                    conn,
                    proposal_id,
                    patch_text=diff_text or None,
                    changed_files=safety.get("changed_files", []),
                    safety={**safety, "patch_generation": patch_generation},
                    status=guard["status"],
                )
                _append_ledger(conn, proposal_id, guard["status"])
                return [_artifact(proposal_id, "created", guard["status"], safety.get("reasons"))]
        diff_text, patch_generation = generate_patch_with_frontier_model(
            payload,
            settings,
            root=root,
            preflight=preflight,
        )
        diff_text = rewrite_diff_paths(diff_text)

    safety = validate_and_scan(payload, diff_text, settings, patch_generation=patch_generation, preflight=preflight)
    invalid_format_repair_history: list[dict[str, Any]] = []
    if safety["decision"] == "invalid_patch_format":
        diff_text, safety, patch_generation, invalid_format_repair_history = _repair_invalid_patch_format_once(
            payload,
            settings,
            diff_text,
            safety,
            patch_generation,
            preflight,
            root=root,
        )
    safety = _with_frontier_usefulness(safety, safety["decision"], patch_generation)
    if invalid_format_repair_history:
        safety["repair_history"] = invalid_format_repair_history
    update_code_evolution_proposal(
        conn,
        proposal_id,
        patch_text=diff_text or None,
        changed_files=safety.get("changed_files", []),
        safety={**safety, "patch_generation": patch_generation},
        status=safety["decision"],
    )
    if not safety["allowed"]:
        _append_ledger(conn, proposal_id, safety["decision"])
        return [_artifact(proposal_id, "created", safety["decision"], safety.get("reasons"))]

    if not cfg.get("auto_merge_paper_only", True):
        update_code_evolution_proposal(conn, proposal_id, status="approved_pending_manual_merge")
        _append_ledger(conn, proposal_id, "approved_pending_manual_merge")
        return [_artifact(proposal_id, "created", "approved_pending_manual_merge")]

    sandbox = run_sandbox_checks(diff_text, payload, settings, root=root)
    repair_history: list[dict[str, Any]] = list(invalid_format_repair_history)
    if not sandbox.get("passed") and diff_text.strip():
        max_repairs = int(cfg.get("patch_repair_attempts", 1))
        repairable_stages = {"patch_check", "patch_apply"}
        if cfg.get("repair_patch_when_sandbox_fails", False):
            repairable_stages.add("tests")
        for attempt in range(1, max_repairs + 1):
            if sandbox.get("passed") or sandbox.get("stage") not in repairable_stages:
                break
            if not cfg.get("repair_patch_when_apply_fails", True):
                break
            repaired_diff, repair_generation = repair_patch_with_frontier_model(
                payload,
                settings,
                diff_text,
                sandbox,
                root=root,
            )
            repaired_diff = rewrite_diff_paths(repaired_diff)
            repair_generation = {**repair_generation, "attempt": attempt}
            repair_entry: dict[str, Any] = {
                "attempt": attempt,
                "previous_stage": sandbox.get("stage"),
                "generation": repair_generation,
            }
            if not repaired_diff.strip():
                repair_entry["result"] = "empty_repair"
                repair_history.append(repair_entry)
                break
            repaired_safety = validate_and_scan(
                payload,
                repaired_diff,
                settings,
                patch_generation=repair_generation,
                preflight=preflight,
            )
            repair_entry["safety"] = repaired_safety
            if not repaired_safety["allowed"]:
                repair_entry["result"] = "blocked_by_safety"
                repair_history.append(repair_entry)
                update_code_evolution_proposal(
                    conn,
                    proposal_id,
                    patch_text=repaired_diff,
                    changed_files=repaired_safety.get("changed_files", []),
                    safety={
                        **_with_frontier_usefulness(repaired_safety, repaired_safety["decision"], repair_generation),
                        "patch_generation": patch_generation,
                        "repair_history": repair_history,
                    },
                    status=repaired_safety["decision"],
                )
                _append_ledger(conn, proposal_id, repaired_safety["decision"])
                return [_artifact(proposal_id, "created", repaired_safety["decision"], repaired_safety.get("reasons"))]
            diff_text = repaired_diff
            safety = repaired_safety
            sandbox = run_sandbox_checks(diff_text, payload, settings, root=root)
            repair_entry["result"] = "passed" if sandbox.get("passed") else "failed"
            repair_entry["sandbox"] = sandbox
            repair_history.append(repair_entry)
        if repair_history:
            update_code_evolution_proposal(
                conn,
                proposal_id,
                patch_text=diff_text,
                changed_files=safety.get("changed_files", []),
                safety={
                    **_with_frontier_usefulness(safety, safety["decision"], patch_generation),
                    "patch_generation": patch_generation,
                    "repair_history": repair_history,
                },
                status=safety["decision"],
            )
    if not sandbox.get("passed"):
        failure_status = classify_sandbox_failure(sandbox)
        safety = _with_frontier_usefulness(safety, failure_status, patch_generation)
        safety["patch_generation"] = patch_generation
        if repair_history:
            safety["repair_history"] = repair_history
        update_code_evolution_proposal(
            conn,
            proposal_id,
            status=failure_status,
            safety=safety,
            tests={**sandbox, "repair_history": repair_history} if repair_history else sandbox,
        )
        _append_ledger(conn, proposal_id, failure_status)
        return [_artifact(proposal_id, "created", failure_status)]

    if cfg.get("git_release_enabled", False):
        release_result = _run_git_release_pipeline(proposal_id, diff_text, payload, settings, safety, sandbox, root)
        release_info = release_result.get("release") or {}
        status = str(release_result.get("status") or "implementation_failed")
        safety = _with_frontier_usefulness(safety, status, patch_generation)
        safety["patch_generation"] = patch_generation
        safety["release"] = release_info
        if repair_history:
            safety["repair_history"] = repair_history
        update_code_evolution_proposal(
            conn,
            proposal_id,
            status=status,
            patch_text=diff_text,
            changed_files=safety.get("changed_files", []),
            safety=safety,
            tests=release_result.get("tests") or {"sandbox": sandbox},
            evaluation=release_result.get("evaluation") or {"release": release_info},
            parent_commit=release_info.get("parent_commit"),
            candidate_commit=release_info.get("candidate_commit"),
            branch_name=release_info.get("branch_name"),
            worktree_path=release_info.get("worktree_path"),
            canary=release_result.get("canary"),
            promotion_reason=release_info.get("promotion_reason"),
            applied_at=_utc_now() if status == "promoted" else None,
            probation_loops_observed=0,
        )
        _append_ledger(conn, proposal_id, status)
        return [_artifact(proposal_id, "created", status)]

    apply_result = apply_patch_to_workspace(
        diff_text,
        root=root,
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
        local_context_patch_apply=bool(cfg.get("local_context_patch_apply", True)),
    )
    tests = {"sandbox": sandbox, "workspace_apply": apply_result}
    if not apply_result.get("applied"):
        safety = _with_frontier_usefulness(safety, "workspace_apply_failed", patch_generation)
        safety["patch_generation"] = patch_generation
        update_code_evolution_proposal(conn, proposal_id, status="workspace_apply_failed", safety=safety, tests=tests)
        _append_ledger(conn, proposal_id, "workspace_apply_failed")
        return [_artifact(proposal_id, "created", "workspace_apply_failed")]

    dependency_install = _install_dependency_manifests_for_diff(
        diff_text,
        root,
        timeout=int(cfg.get("sandbox_timeout_seconds", 120)),
        enabled=bool(cfg.get("install_changed_python_dependencies", True)),
    )
    if dependency_install:
        tests["workspace_dependency_install"] = dependency_install
    if any(result["returncode"] != 0 for result in dependency_install):
        safety = _with_frontier_usefulness(safety, "dependency_install_failed", patch_generation)
        safety["patch_generation"] = patch_generation
        update_code_evolution_proposal(conn, proposal_id, status="dependency_install_failed", safety=safety, tests=tests)
        _append_ledger(conn, proposal_id, "dependency_install_failed")
        return [_artifact(proposal_id, "created", "dependency_install_failed")]

    probation_status = WORKSPACE_PROBATION_STATUS if cfg.get("workspace_status_names", True) else LEGACY_PROBATION_STATUS
    safety = _with_frontier_usefulness(safety, probation_status, patch_generation)
    safety["patch_generation"] = patch_generation
    if repair_history:
        safety["repair_history"] = repair_history
    update_code_evolution_proposal(
        conn,
        proposal_id,
        status=probation_status,
        patch_text=diff_text,
        changed_files=safety.get("changed_files", []),
        safety=safety,
        tests=tests,
        applied_at=_utc_now(),
        probation_loops_observed=0,
    )
    _append_ledger(conn, proposal_id, probation_status)
    return [_artifact(proposal_id, "created", probation_status)]


def _artifact(proposal_id: str, action_status: str, status: str, reasons: list[str] | None = None) -> dict:
    return {
        "artifact_type": "code_evolution",
        "proposal_id": proposal_id,
        "action_status": action_status,
        "status": status,
        "reasons": reasons or [],
        "report": str(REPORT_MD),
    }


def evaluate_code_evolution(conn: Any, settings: dict, root: pathlib.Path = ROOT) -> list[dict]:
    cfg = _cfg(settings)
    evaluated = []
    for row in code_evolution_by_status(conn, sorted(PROBATION_STATUSES), limit=20):
        loops = int(row.get("probation_loops_observed") or 0) + 1
        evaluation = dict(row.get("evaluation") or {})
        evaluation["last_checked_at"] = _utc_now()
        evaluation["live_trading_allowed"] = bool(settings.get("allow_live_trading", False))
        status = row.get("status") or WORKSPACE_PROBATION_STATUS
        decision = "continue_probation"
        if settings.get("allow_live_trading"):
            decision = "revert_live_flag"
            status = "revert_required"
        elif loops >= int(cfg.get("probation_loops", 3)):
            decision = "keep"
            status = WORKSPACE_KEPT_STATUS if cfg.get("workspace_status_names", True) else LEGACY_KEPT_STATUS
        update_code_evolution_proposal(
            conn,
            row["proposal_id"],
            status=status,
            evaluation=evaluation,
            probation_loops_observed=loops,
        )
        if status == "revert_required" and cfg.get("rollback_on_health_failure", True) and row.get("patch_text"):
            revert = reverse_patch_in_workspace(row["patch_text"], root=root)
            evaluation["revert"] = revert
            final_status = "reverted" if revert.get("reverted") else "revert_failed"
            update_code_evolution_proposal(conn, row["proposal_id"], status=final_status, evaluation=evaluation)
            status = final_status
        _append_ledger(conn, row["proposal_id"], status)
        evaluated.append({"proposal_id": row["proposal_id"], "status": status, "decision": decision})
    return evaluated


def normalize_code_evolution_statuses(conn: Any) -> dict:
    updated: dict[str, int] = {}
    for row in code_evolution_by_status(conn, ["blocked_human_review"], limit=1000):
        safety = row.get("safety") or {}
        reasons = safety.get("reasons") or []
        patch_generation = safety.get("patch_generation") or {}
        new_status = _safety_decision(list(reasons), patch_generation=patch_generation, preflight=safety.get("preflight"))
        if new_status in {"auto_allowed", "blocked_human_review"}:
            continue
        update_code_evolution_proposal(conn, row["proposal_id"], status=new_status, safety=safety)
        updated[new_status] = updated.get(new_status, 0) + 1
    for row in code_evolution_by_status(conn, ["no_changed_files"], limit=2000):
        safety = row.get("safety") or {}
        reasons = list(safety.get("reasons") or [])
        patch_generation = safety.get("patch_generation") or {}
        patch_text = str(row.get("patch_text") or "")
        new_status = None
        unavailable_reason = _patch_generation_unavailable_reason(patch_generation)
        if unavailable_reason:
            reasons = sorted(set([reason for reason in reasons if reason != "no_changed_files"] + [f"patch_generation_unavailable:{unavailable_reason}"]))
            new_status = "patch_generation_unavailable_retry_later"
        elif patch_text.strip() and not changed_files_from_diff(patch_text):
            reasons = sorted(set([reason for reason in reasons if reason != "no_changed_files"] + ["invalid_patch_format"]))
            new_status = "invalid_patch_format"
        if not new_status:
            continue
        updated_safety = dict(safety)
        updated_safety["reasons"] = reasons
        updated_safety["decision"] = new_status
        if patch_generation:
            updated_safety["patch_generation"] = patch_generation
        update_code_evolution_proposal(conn, row["proposal_id"], status=new_status, safety=updated_safety)
        updated[new_status] = updated.get(new_status, 0) + 1
    for status in ("patch_generation_unavailable_retry_later", "invalid_patch_format"):
        for row in code_evolution_by_status(conn, [status], limit=2000):
            safety = row.get("safety") or {}
            reasons = list(safety.get("reasons") or [])
            if "no_changed_files" not in reasons:
                continue
            updated_safety = dict(safety)
            updated_safety["reasons"] = [reason for reason in reasons if reason != "no_changed_files"]
            update_code_evolution_proposal(conn, row["proposal_id"], status=status, safety=updated_safety)
            updated[f"{status}_reason_cleanup"] = updated.get(f"{status}_reason_cleanup", 0) + 1
    return updated


def code_evolution_summary(conn: Any) -> dict:
    rows = code_evolution_recent(conn, limit=50)
    counts: dict[str, int] = {}
    failure_causes: dict[str, int] = {}
    failure_class_counts: dict[str, int] = {
        "invalid_paths": 0,
        "invalid_tests": 0,
        "stale_hunks_or_patch_apply": 0,
        "no_op_or_empty_patch": 0,
        "malformed_diff": 0,
        "safety_blocks": 0,
    }
    implementation_mode_counts: dict[str, int] = {}
    model_tier_counts: dict[str, int] = {}
    frontier_waste_reasons: dict[str, int] = {}
    canary_stage_counts: dict[str, int] = {}
    repaired_path_count = 0
    preflight_reject_count = 0
    repair_attempt_count = 0
    repair_success_count = 0
    quality_scores: list[float] = []
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        canary = row.get("canary") if isinstance(row.get("canary"), dict) else {}
        canary_stage = str(canary.get("stage") or "none")
        canary_stage_counts[canary_stage] = canary_stage_counts.get(canary_stage, 0) + 1
        safety = row.get("safety") or {}
        preflight = safety.get("preflight") or {}
        mode = (
            safety.get("implementation_mode")
            or preflight.get("implementation_mode")
            or (row.get("payload") or {}).get("implementation_mode")
            or "unknown"
        )
        implementation_mode_counts[str(mode)] = implementation_mode_counts.get(str(mode), 0) + 1
        patch_generation = safety.get("patch_generation") or {}
        tier = str(patch_generation.get("requested_model_tier") or patch_generation.get("model_tier") or row.get("model_tier") or "unknown")
        model_tier_counts[tier] = model_tier_counts.get(tier, 0) + 1
        wasted = safety.get("frontier_call_wasted_reason")
        if wasted:
            frontier_waste_reasons[str(wasted)] = frontier_waste_reasons.get(str(wasted), 0) + 1
        for reason in safety.get("reasons", []) or []:
            failure_causes[reason] = failure_causes.get(reason, 0) + 1
            if "invalid_target" in reason or "invalid_target_files" in reason or reason.startswith("path_not_allowed"):
                failure_class_counts["invalid_paths"] += 1
            if "invalid_test" in reason:
                failure_class_counts["invalid_tests"] += 1
            if "patch_apply" in reason or "stale" in reason:
                failure_class_counts["stale_hunks_or_patch_apply"] += 1
            if reason in {"no_changed_files", "missing_unified_diff"}:
                failure_class_counts["no_op_or_empty_patch"] += 1
            if "invalid_patch_format" in reason:
                failure_class_counts["malformed_diff"] += 1
            if reason in {
                "enables_live_trading",
                "touches_credentials",
                "broker_write_or_order_api",
                "real_notional_increase",
                "startup_or_system_task",
                "destructive_filesystem_action",
                "installer_command_in_code",
            }:
                failure_class_counts["safety_blocks"] += 1
        if preflight.get("path_repairs"):
            repaired_path_count += 1
        scorecard = safety.get("proposal_scorecard") or preflight.get("quality_scorecard") or {}
        if scorecard.get("reject_before_model_call"):
            preflight_reject_count += 1
        if scorecard.get("repair_attempted"):
            repair_attempt_count += 1
        if scorecard.get("repair_successful"):
            repair_success_count += 1
        if scorecard.get("proposal_quality_score") is not None:
            try:
                quality_scores.append(float(scorecard["proposal_quality_score"]))
            except (TypeError, ValueError):
                pass
    useful = sum(counts.get(status, 0) for status in SUCCESS_STATUSES)
    attempted = len(rows)
    tracked_failures = sum(failure_class_counts.values())
    return {
        "report": str(REPORT_MD),
        "ledger": str(LEDGER_JSONL),
        "recent_count": len(rows),
        "status_counts": counts,
        "implementation_mode_counts": implementation_mode_counts,
        "model_tier_counts": model_tier_counts,
        "canary_stage_counts": canary_stage_counts,
        "frontier_waste_reasons": frontier_waste_reasons,
        "failure_cause_counts": failure_causes,
        "failure_benchmark": {
            "class_counts": failure_class_counts,
            "tracked_failure_count": tracked_failures,
            "useful_merge_count": useful,
            "solve_rate": round(useful / (useful + tracked_failures), 3) if useful + tracked_failures else None,
        },
        "path_repair_proposal_count": repaired_path_count,
        "preflight_reject_count": preflight_reject_count,
        "proposal_quality_avg": round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else None,
        "repair_attempt_count": repair_attempt_count,
        "repair_success_rate": round(repair_success_count / repair_attempt_count, 3) if repair_attempt_count else None,
        "useful_merge_rate_recent": round(useful / attempted, 3) if attempted else None,
        "latest": [
            {
                "proposal_id": row["proposal_id"],
                "title": row["title"],
                "category": row["category"],
                "status": row["status"],
                "changed_files": row.get("changed_files", []),
                "implementation_mode": (
                    ((row.get("safety") or {}).get("implementation_mode"))
                    or (((row.get("safety") or {}).get("preflight") or {}).get("implementation_mode"))
                    or (row.get("payload") or {}).get("implementation_mode")
                ),
                "model_tier": (
                    (((row.get("safety") or {}).get("patch_generation") or {}).get("requested_model_tier"))
                    or (((row.get("safety") or {}).get("patch_generation") or {}).get("model_tier"))
                    or row.get("model_tier")
                ),
                "frontier_call_useful": (row.get("safety") or {}).get("frontier_call_useful"),
                "frontier_call_wasted_reason": (row.get("safety") or {}).get("frontier_call_wasted_reason"),
                "proposal_quality_score": ((row.get("safety") or {}).get("proposal_scorecard") or {}).get(
                    "proposal_quality_score"
                ),
                "canary_stage": ((row.get("canary") or {}).get("stage") if isinstance(row.get("canary"), dict) else None),
                "updated_at": row["updated_at"],
            }
            for row in rows[:10]
        ],
    }


def write_code_evolution_reports(conn: Any, settings: dict | None = None) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    normalized_statuses = normalize_code_evolution_statuses(conn)
    rows = code_evolution_recent(conn, limit=100)
    summary = code_evolution_summary(conn)
    report = {
        "generated_at": _utc_now(),
        "enabled": _cfg(settings or {}).get("enabled", True),
        "normalized_statuses": normalized_statuses,
        "summary": summary,
        "recent": rows,
        "hard_blocks": [
            "live_trading",
            "credentials",
            "broker_order_apis",
            "real_notional_increase",
            "startup_or_system_tasks",
            "destructive_data_actions",
            "unknown_dependency_installs",
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# AI Code Evolution Report",
        "",
        "This report tracks frontier-model code changes that pass through the deterministic Build Governor.",
        "",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Ledger: `{summary.get('ledger')}`",
        f"- Status counts: `{summary.get('status_counts', {})}`",
        f"- Implementation modes: `{summary.get('implementation_mode_counts', {})}`",
        f"- Model tiers: `{summary.get('model_tier_counts', {})}`",
        f"- Canary stages: `{summary.get('canary_stage_counts', {})}`",
        f"- Frontier waste reasons: `{summary.get('frontier_waste_reasons', {})}`",
        f"- Normalized old statuses this report: `{report.get('normalized_statuses', {})}`",
        f"- Failure causes: `{summary.get('failure_cause_counts', {})}`",
        f"- Failure benchmark: `{summary.get('failure_benchmark', {})}`",
        f"- Path repair proposal count: `{summary.get('path_repair_proposal_count', 0)}`",
        f"- Preflight reject count: `{summary.get('preflight_reject_count', 0)}`",
        f"- Proposal quality avg: `{summary.get('proposal_quality_avg')}`",
        f"- Repair success rate: `{summary.get('repair_success_rate')}`",
        f"- Useful merge rate recent: `{summary.get('useful_merge_rate_recent')}`",
        "",
        "## Recent Proposals",
        "",
    ]
    recent = report.get("recent", [])
    if not recent:
        lines.append("No code-evolution proposals yet.")
    for row in recent[:30]:
        safety = row.get("safety") or {}
        lines.append(
            f"- `{row.get('proposal_id')}` P{row.get('priority')} `{row.get('category')}` "
            f"mode=`{((row.get('safety') or {}).get('implementation_mode') or ((row.get('safety') or {}).get('preflight') or {}).get('implementation_mode'))}` "
            f"status=`{row.get('status')}` files=`{row.get('changed_files', [])}`"
        )
        if safety.get("reasons"):
            lines.append(f"  - Reasons: `{safety.get('reasons')}`")
        scorecard = safety.get("proposal_scorecard") or {}
        if scorecard:
            lines.append(
                "  - Scorecard: "
                f"quality=`{scorecard.get('proposal_quality_score')}` "
                f"mode=`{scorecard.get('implementation_mode')}` "
                f"target=`{scorecard.get('target_path_status')}` "
                f"tests=`{scorecard.get('test_command_status')}` "
                f"runtime=`{scorecard.get('runtime_integration_status')}`"
            )
        patch_generation = safety.get("patch_generation") or {}
        if patch_generation or safety.get("frontier_call_useful") is not None:
            lines.append(
                "  - Model: "
                f"tier=`{patch_generation.get('requested_model_tier') or patch_generation.get('model_tier')}` "
                f"frontier_useful=`{safety.get('frontier_call_useful')}` "
                f"wasted_reason=`{safety.get('frontier_call_wasted_reason')}`"
            )
    return "\n".join(lines) + "\n"


def _append_ledger(conn: Any, proposal_id: str, event: str) -> None:
    rows = [row for row in code_evolution_recent(conn, limit=100) if row["proposal_id"] == proposal_id]
    payload = {
        "event": event,
        "recorded_at": _utc_now(),
        "proposal": rows[0] if rows else {"proposal_id": proposal_id},
    }
    ledger_path = _ledger_path_for_connection(conn)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _ledger_path_for_connection(conn: Any) -> pathlib.Path:
    default_ledger = RUNS_DIR / "evolution_ledger.jsonl"
    if LEDGER_JSONL != default_ledger:
        return LEDGER_JSONL
    try:
        row = conn.execute("pragma database_list").fetchone()
        db_path = pathlib.Path(str(row[2])).resolve() if row and row[2] else None
    except Exception:  # noqa: BLE001
        db_path = None
    if db_path and db_path != (RUNS_DIR / "radar.sqlite").resolve():
        return db_path.parent / "evolution_ledger.jsonl"
    return LEDGER_JSONL

