"""Validated ingestion boundary for paper-only LLM recommendations.

This module intentionally has no provider, broker, or order-writing imports.
It accepts already-returned provider responses, normalizes validated
recommendations, and exposes a small dispatch boundary with durable
deduplication support.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


SCHEMA_VERSION = "paper-recommendation.v1"
MAX_RAW_PREVIEW_CHARS = 800
DEFAULT_RETRY_TIMEOUT_SECONDS = 5.0

ACTION_ALIASES = {
    "code": "code_change",
    "change_code": "code_change",
    "modify_code": "code_change",
    "recommend_code_change": "code_change",
    "propose_code_change": "code_change",
    "implementation": "code_change",
    "diagnose": "diagnostic",
    "propose_diagnostic": "diagnostic",
    "analysis": "diagnostic",
    "research": "market_research",
    "propose_market_research": "market_research",
    "add_market": "market_data_change",
    "propose_market_data_change": "market_data_change",
    "market_data": "market_data_change",
    "strategy": "strategy_change",
    "propose_strategy_change": "strategy_change",
    "adjust_strategy": "strategy_change",
    "propose_strategy_lab_experiment": "strategy_lab_experiment",
    "strategy_lab_experiment": "strategy_lab_experiment",
    "quality": "quality_control",
    "propose_quality_control": "quality_control",
    "propose_report_change": "report_change",
    "propose_test_change": "test_change",
    "propose_paper_experiment": "paper_experiment",
    "propose_route_review": "route_review",
    "no-op": "no_action",
    "noop": "no_action",
    "propose_no_action": "no_action",
}

ALLOWED_ACTIONS = {
    "code_change",
    "diagnostic",
    "market_research",
    "market_data_change",
    "strategy_change",
    "strategy_lab_experiment",
    "quality_control",
    "report_change",
    "test_change",
    "paper_experiment",
    "route_review",
    "no_action",
}

ALLOWED_ROLES = {
    "analyst",
    "code_evolution",
    "code_reviewer",
    "data_quality",
    "diagnostic",
    "execution_analyst",
    "market_analyst",
    "market_researcher",
    "market_structure",
    "paper_trader",
    "portfolio_manager",
    "quality_reviewer",
    "researcher",
    "risk",
    "risk_manager",
    "route_resolver",
    "self_improvement",
    "strategy",
    "strategy_researcher",
    "swarm",
    "system",
}

DOWNSTREAM_TASK_TYPES = {
    "code_change": "code_evolution",
    "test_change": "code_evolution",
    "market_data_change": "market_data",
    "strategy_change": "paper_experiment",
    "strategy_lab_experiment": "paper_experiment",
    "paper_experiment": "paper_experiment",
    "route_review": "route_review",
    "market_research": "research",
    "quality_control": "diagnostic",
    "report_change": "diagnostic",
    "diagnostic": "diagnostic",
    "no_action": "no_action",
}

REPAIRABLE_PREFLIGHT_FAILURES = {
    "missing_unified_diff",
    "no_changed_files",
    "invalid_target_files",
    "invalid_test_commands",
}

_REQUIRED_TEXT_FIELDS = ("title", "rationale")
_PROHIBITED_TARGET_PARTS = {
    ".git",
    ".github/workflows",
    "systemd",
    "launchd",
    "startup",
    "credentials",
    "secrets",
    "authorized_keys",
}
_DEPENDENCY_FILES = {"requirements-autonomous.txt", "requirements-llm.txt"}
_ALLOWED_TARGET_SUFFIXES = {
    ".py",
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
}
_TEST_COMMAND_PREFIXES = (
    "python -m unittest",
    "python3 -m unittest",
    "python -m pytest",
    "python3 -m pytest",
    "pytest",
)


def _json_default(value: Any) -> str:
    return repr(value)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _raw_audit(value: Any) -> Dict[str, str]:
    if isinstance(value, str):
        raw = value
    else:
        raw = _stable_json(value)
    return {
        "raw_preview": raw[:MAX_RAW_PREVIEW_CHARS],
        "raw_digest": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
    }


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _single_top_level_object_text(text: str) -> Optional[str]:
    stripped = _strip_markdown_fences(text)
    if not stripped or stripped.startswith("[") or not stripped.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(value, Mapping) or stripped[end:].strip():
        return None
    return stripped[:end]


def _clean_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    return re.sub(r"[\s-]+", "_", role)


def _clean_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    action = re.sub(r"[\s-]+", "_", action)
    return ACTION_ALIASES.get(action, action)


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != [] and value != {}


def _repair_execution_route_hunter_payload(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    market_key = str(_first(value, ("market_key", "market"), "") or "").strip()
    if market_key != "paper.execution_route_hunter":
        return None

    proposed_change = _first(value, ("proposed_change", "proposed change", "change"))
    evidence = value.get("evidence")
    if not _nonempty(proposed_change) and not _nonempty(evidence):
        return None

    repaired: Dict[str, Any] = dict(value)
    action = _clean_action(_first(repaired, ("action", "proposed_action"), "code_change"))
    if action not in ALLOWED_ACTIONS:
        action = "code_change"
    repaired["action"] = action

    try:
        priority = int(repaired.get("priority"))
    except (TypeError, ValueError):
        priority = 90
    repaired["priority"] = max(1, min(100, priority))

    if not _nonempty(repaired.get("title")):
        repaired["title"] = "Harden execution_route_hunter paper recommendation output"
    if not _nonempty(repaired.get("rationale")):
        repaired["rationale"] = (
            "Preserve parser compatibility by requiring execution_route_hunter "
            "to emit one complete paper-only recommendation object."
        )
    if not _nonempty(repaired.get("proposed_change")):
        repaired["proposed_change"] = (
            "Require a single schema-complete paper-only JSON recommendation object "
            "for execution_route_hunter responses."
        )
    if not _nonempty(repaired.get("evidence")):
        repaired["evidence"] = {
            "issue": "Incomplete execution_route_hunter recommendation object threatened parser compatibility.",
            "impact": "Recommendation ingestion could fail before any paper-routing review.",
        }
    return repaired


def _repair_cross_market_researcher_payload(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    source_agent = _clean_role(_first(value, ("source_agent", "agent_name", "agent"), ""))
    market_key = str(_first(value, ("market_key", "market"), "") or "").strip()
    if source_agent != "cross_market_researcher" and market_key != "paper_global_macro":
        return None

    proposed_change = _first(value, ("proposed_change", "proposed change", "change"))
    evidence = value.get("evidence")
    if not _nonempty(proposed_change) and not _nonempty(evidence) and not _nonempty(value.get("rationale")):
        return None

    repaired: Dict[str, Any] = dict(value)
    action = _clean_action(_first(repaired, ("action", "proposed_action"), "diagnostic"))
    if action not in {"diagnostic", "no_action"}:
        action = "diagnostic"
    repaired["action"] = action

    try:
        priority = int(repaired.get("priority"))
    except (TypeError, ValueError):
        priority = 90
    repaired["priority"] = max(1, min(100, priority))

    if not _nonempty(repaired.get("title")):
        repaired["title"] = "Return a single complete paper-trading recommendation object"
    if not _nonempty(repaired.get("rationale")):
        repaired["rationale"] = (
            "Preserve parser compatibility by requiring cross_market_researcher to emit "
            "exactly one schema-complete paper-only recommendation object."
        )
    if not _nonempty(repaired.get("market_key")):
        repaired["market_key"] = "paper_global_macro"

    default_evidence: Dict[str, Any] = {
        "issue": "Incomplete cross_market_researcher recommendation object prevented strict downstream parsing.",
        "impact": "No actionable paper-trading recommendation reached the paper-only decision flow.",
        "constraint": "Output must remain paper-only and contain exactly one JSON object.",
    }
    current_evidence = repaired.get("evidence")
    if not _nonempty(current_evidence):
        repaired["evidence"] = default_evidence
    elif isinstance(current_evidence, Mapping):
        repaired["evidence"] = {**default_evidence, **dict(current_evidence)}

    default_proposed_change: Dict[str, Any] = {
        "format_rule": "No markdown, no commentary, no arrays, valid JSON only.",
        "goal": "Enforce a strict single-object schema for all cross_market_researcher responses.",
        "required_fields": "action, priority, title, rationale, market_key, evidence, proposed_change",
        "safety_rule": "Paper-trading only; do not imply live execution.",
    }
    current_proposed_change = repaired.get("proposed_change")
    if not _nonempty(current_proposed_change):
        repaired["proposed_change"] = default_proposed_change
    elif isinstance(current_proposed_change, Mapping):
        repaired["proposed_change"] = {**default_proposed_change, **dict(current_proposed_change)}
    return repaired


def _repair_known_recommendation_payload(value: Any) -> Optional[Dict[str, Any]]:
    for repair in (_repair_execution_route_hunter_payload, _repair_cross_market_researcher_payload):
        repaired = repair(value)
        if repaired is not None:
            return repaired
    return None


def _looks_like_recommendation(value: Any) -> bool:
    repaired = _repair_known_recommendation_payload(value)
    if repaired is not None:
        return True
    if not isinstance(value, Mapping):
        return False
    keys = {str(key).lower().replace(" ", "_") for key in value}
    return bool({"action", "proposed_action"} & keys) and "title" in keys


def _native_payload(response: Any) -> Optional[Mapping[str, Any]]:
    """Return only genuinely structured/native payloads.

    Objects explicitly marked as fallback are not accepted here. This prevents
    old priority-50 fallback wrappers from overriding complete JSON embedded in
    their rationale or proposed change.
    """

    if not isinstance(response, Mapping):
        return None
    parser = str(response.get("parser", "")).strip().lower()
    if parser == "fallback":
        return None
    repaired = _repair_known_recommendation_payload(response)
    if repaired is not None:
        return repaired

    for key in ("output_parsed", "parsed", "structured_output"):
        candidate = response.get(key)
        repaired = _repair_known_recommendation_payload(candidate)
        if repaired is not None:
            return repaired
        if _looks_like_recommendation(candidate):
            return candidate
        if isinstance(candidate, Mapping):
            nested = candidate.get("recommendation")
            repaired = _repair_known_recommendation_payload(nested)
            if repaired is not None:
                return repaired

            if _looks_like_recommendation(nested):
                return nested

    candidate = response.get("recommendation")
    repaired = _repair_known_recommendation_payload(candidate)
    if repaired is not None:
        return repaired
    if _looks_like_recommendation(candidate):
        return candidate

    if _looks_like_recommendation(response):
        return response
    return None


def _provider_texts(response: Any) -> List[str]:
    texts: List[str] = []
    seen: Set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        raw = value.strip()
        if not raw:
            return
        for candidate in (_single_top_level_object_text(raw), raw):
            if isinstance(candidate, str) and candidate and candidate not in seen:
                seen.add(candidate)
                texts.append(candidate)

    if isinstance(response, str):
        add(response)
        return texts
    if not isinstance(response, Mapping):
        return texts

    # Full provider response fields precede report-preview/fallback fields.
    for key in (
        "raw_response",
        "provider_response",
        "response_text",
        "output_text",
        "text",
        "content",
    ):
        value = response.get(key)
        if isinstance(value, str):
            add(value)

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            add(item.get("text"))
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping):
                        add(part.get("text"))
                        add(part.get("output_text"))

    # Legacy fallback wrappers commonly place the intended object here.
    for key in ("proposed_change", "proposed change", "rationale", "preview"):
        add(response.get(key))
    return texts


def _json_objects(text: str) -> Iterable[Mapping[str, Any]]:
    """Yield complete JSON objects without guessing or repairing delimiters."""

    stripped = _strip_markdown_fences(text).strip()
    if not stripped:
        return
    if stripped.startswith("["):
        return

    decoder = json.JSONDecoder()
    strict_object = _single_top_level_object_text(stripped)
    if strict_object is not None:
        try:
            value, end = decoder.raw_decode(strict_object)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if end != len(strict_object) or not isinstance(value, Mapping):
            return
        repaired = _repair_known_recommendation_payload(value)
        yield repaired or value
        nested = value.get("recommendation")
        if isinstance(nested, Mapping):
            repaired_nested = _repair_known_recommendation_payload(nested)
            yield repaired_nested or nested
        return

    starts: List[int] = []
    if stripped.startswith("{"):
        starts.append(text.find("{"))
    starts.extend(match.start() for match in re.finditer(r"\{", text))
    visited: Set[int] = set()
    for start in starts:
        if start < 0 or start in visited:
            continue
        visited.add(start)
        try:
            value, _ = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            repaired = _repair_known_recommendation_payload(value)
            yield repaired or value
            nested = value.get("recommendation")
            if isinstance(nested, Mapping):
                repaired_nested = _repair_known_recommendation_payload(nested)
                yield repaired_nested or nested


def _appears_truncated(text: str) -> bool:
    stripped = text.strip()
    if not stripped or "{" not in stripped:
        return False
    in_string = False
    escaped = False
    depth = 0
    saw_object = False
    for char in stripped[stripped.find("{") :]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
            saw_object = True
        elif char == "}":
            depth -= 1
    return saw_object and (depth > 0 or in_string)


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unsafe_text_reason(payload: Mapping[str, Any]) -> Optional[str]:
    proposed = _first(payload, ("proposed_change", "proposed change", "change"), "")
    fields = [
        str(payload.get("title", "")),
        _stable_json(proposed) if not isinstance(proposed, str) else proposed,
    ]
    text = "\n".join(fields).lower()

    checks = (
        (
            "live_trading",
            r"(?:enable|activate|turn\s+on|set)\s+(?:actual\s+|real\s+)?live[\s_-]*trading"
            r"|live_trading_allowed\s*[:=]\s*true",
        ),
        (
            "credential_access",
            r"(?:obtain|read|write|store|expose|rotate|request|use)\s+(?:api\s+)?"
            r"(?:credentials?|secrets?|private\s+keys?|passwords?|tokens?)",
        ),
        (
            "broker_order_write",
            r"(?:submit|send|place|write|execute)\s+(?:a\s+|real\s+|live\s+)?"
            r"(?:broker\s+)?orders?",
        ),
        (
            "account_capability_change",
            r"(?:enable|activate|upgrade|change|set)\s+(?:broker\s+|account\s+)?"
            r"(?:margin|options|shorting|account[\s_-]*capabilit)",
        ),
        (
            "startup_or_system_task",
            r"(?:create|install|enable|modify|register)\s+(?:a\s+)?"
            r"(?:startup|systemd|launchd|cron|scheduled\s+task|system\s+service)",
        ),
        (
            "destructive_action",
            r"(?:rm\s+-rf|drop\s+(?:table|database)|truncate\s+table|"
            r"delete\s+all|wipe\s+(?:the\s+)?(?:database|data))",
        ),
        (
            "real_notional_increase",
            r"(?:increase|raise|expand)\s+(?:the\s+)?(?:real|live|actual)\s+notional",
        ),
        (
            "installer_command",
            r"(?:^|[\s;&|])(?:sudo\s+)?(?:pip3?|python\d*\s+-m\s+pip|"
            r"apt(?:-get)?|yum|dnf|brew|npm)\s+install\b"
            r"|curl\b[^\n|]*\|\s*(?:ba)?sh\b",
        ),
    )
    for reason, pattern in checks:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            return reason
    return None


def _validate_target_files(payload: Mapping[str, Any]) -> Optional[str]:
    targets = _as_string_list(
        _first(payload, ("target_files", "files", "expected_files"), [])
    )
    for target in targets:
        normalized = target.replace("\\", "/")
        lowered = normalized.lower()
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            return "invalid_target_files"
        if any(part in lowered for part in _PROHIBITED_TARGET_PARTS):
            return "invalid_target_files"
        name = path.name.lower()
        if name.startswith("requirements") and name not in _DEPENDENCY_FILES:
            return "invalid_target_files"
        if path.suffix.lower() not in _ALLOWED_TARGET_SUFFIXES:
            return "invalid_target_files"
    return None


def _validate_test_commands(payload: Mapping[str, Any]) -> Optional[str]:
    commands = _as_string_list(
        _first(payload, ("tests_to_run", "test_commands", "tests"), [])
    )
    for command in commands:
        lowered = command.strip().lower()
        if any(token in command for token in (";", "&&", "||", "`", "$(", "\n", "\r")):
            return "invalid_test_commands"
        if not lowered.startswith(_TEST_COMMAND_PREFIXES):
            return "invalid_test_commands"
    return None


def _canonical_evidence(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)


def _normalize_payload(
    payload: Mapping[str, Any],
    *,
    parse_status: str,
    raw_response: Any,
    agent_role: Optional[str],
    model_metadata: Optional[Mapping[str, Any]],
    provenance: Optional[Mapping[str, Any]],
    parent_recommendation_id: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    action = _clean_action(_first(payload, ("action", "proposed_action", "type")))
    role = _clean_role(
        _first(payload, ("agent_role", "role"), agent_role)
    )
    if action not in ALLOWED_ACTIONS:
        return None, "invalid_schema", "invalid_action"
    if role not in ALLOWED_ROLES:
        return None, "role_rejected", "invalid_agent_role"

    missing = [field for field in _REQUIRED_TEXT_FIELDS if not _nonempty(payload.get(field))]
    proposed_change = _first(
        payload,
        ("proposed_change", "proposed change", "change", "implementation"),
    )
    if action != "no_action" and not _nonempty(proposed_change):
        missing.append("proposed_change")
    if missing:
        return None, "invalid_schema", "missing_" + "_".join(missing)

    try:
        priority = int(payload.get("priority"))
    except (TypeError, ValueError):
        return None, "invalid_schema", "invalid_priority"
    if not 1 <= priority <= 100:
        return None, "invalid_schema", "invalid_priority"

    safety_reason = _unsafe_text_reason(payload)
    if safety_reason:
        return None, "safety_rejected", safety_reason
    target_reason = _validate_target_files(payload)
    if target_reason:
        return None, "safety_rejected", target_reason
    test_reason = _validate_test_commands(payload)
    if test_reason:
        return None, "safety_rejected", test_reason

    inherited_model = model_metadata or {}
    payload_model = payload.get("model_metadata")
    merged_model: Dict[str, Any] = dict(inherited_model)
    if isinstance(payload_model, Mapping):
        merged_model.update(payload_model)
    for key in ("model", "provider", "request_id"):
        if key in payload and key not in merged_model:
            merged_model[key] = payload[key]

    inherited_provenance = provenance or {}
    payload_provenance = payload.get("provenance")
    merged_provenance: Dict[str, Any] = dict(inherited_provenance)
    if isinstance(payload_provenance, Mapping):
        merged_provenance.update(payload_provenance)

    target_files = _as_string_list(
        _first(payload, ("target_files", "files", "expected_files"), [])
    )
    tests_to_run = _as_string_list(
        _first(payload, ("tests_to_run", "test_commands", "tests"), [])
    )
    normalized: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "priority": priority,
        "title": str(payload["title"]).strip(),
        "rationale": str(payload["rationale"]).strip(),
        "evidence": _canonical_evidence(payload.get("evidence")),
        "proposed_change": proposed_change,
        "market_key": _first(payload, ("market_key", "market", "inst_id")),
        "agent_role": role,
        "model_metadata": merged_model,
        "provenance": merged_provenance,
        "parse_status": parse_status,
        "parent_recommendation_id": _first(
            payload,
            ("parent_recommendation_id", "parent_id"),
            parent_recommendation_id,
        ),
        "target_files": target_files,
        "tests_to_run": tests_to_run,
        "downstream_task_type": DOWNSTREAM_TASK_TYPES[action],
    }
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "priority": priority,
        "title": normalized["title"],
        "rationale": normalized["rationale"],
        "evidence": normalized["evidence"],
        "proposed_change": proposed_change,
        "market_key": normalized["market_key"],
        "agent_role": role,
        "target_files": target_files,
        "tests_to_run": tests_to_run,
    }
    canonical = _stable_json(identity_payload)
    normalized["canonical_payload_hash"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    normalized["recommendation_id"] = "rec_" + normalized["canonical_payload_hash"][:24]
    normalized.update(_raw_audit(raw_response))
    return normalized, parse_status, None


def normalize_recommendation(
    response: Any,
    *,
    agent_role: Optional[str] = None,
    model_metadata: Optional[Mapping[str, Any]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
    parent_recommendation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one provider response without semantic fallback.

    The result always contains ``accepted`` and ``parse_status``. Rejected
    responses contain only bounded raw audit material and never contain a
    downstream task.
    """

    audit = _raw_audit(response)
    if response is None or response == "" or response == {}:
        return {
            "schema_version": SCHEMA_VERSION,
            "accepted": False,
            "quarantined": True,
            "parse_status": "empty_response",
            "terminal_failure_reason": "empty_response",
            **audit,
        }

    native = _native_payload(response)
    if native is not None:
        normalized, status, reason = _normalize_payload(
            native,
            parse_status="native_valid",
            raw_response=response,
            agent_role=agent_role,
            model_metadata=model_metadata,
            provenance=provenance,
            parent_recommendation_id=parent_recommendation_id,
        )
        if normalized is not None:
            normalized.update({"accepted": True, "quarantined": False})
            return normalized
        return {
            "schema_version": SCHEMA_VERSION,
            "accepted": False,
            "quarantined": True,
            "parse_status": status,
            "terminal_failure_reason": reason,
            **audit,
        }

    texts = _provider_texts(response)
    saw_complete_json = False
    last_status = "invalid_schema"
    last_reason = "no_structured_recommendation"
    for text in texts:
        for recovered in _json_objects(text):
            saw_complete_json = True
            if not _looks_like_recommendation(recovered):
                continue
            normalized, status, reason = _normalize_payload(
                recovered,
                parse_status="recovered_valid",
                raw_response=response,
                agent_role=agent_role,
                model_metadata=model_metadata,
                provenance=provenance,
                parent_recommendation_id=parent_recommendation_id,
            )
            if normalized is not None:
                normalized.update({"accepted": True, "quarantined": False})
                return normalized
            last_status, last_reason = status, reason or last_reason

    if any(_appears_truncated(text) for text in texts):
        last_status, last_reason = "truncated_json", "truncated_json"
    elif not texts:
        last_status, last_reason = "empty_response", "empty_response"
    elif saw_complete_json and last_reason == "no_structured_recommendation":
        last_reason = "invalid_schema"

    return {
        "schema_version": SCHEMA_VERSION,
        "accepted": False,
        "quarantined": True,
        "parse_status": last_status,
        "terminal_failure_reason": last_reason,
        **audit,
    }


def schema_retry_prompt() -> str:
    return (
        "Return exactly one complete JSON object. Required fields: action, "
        "priority (integer 1-100), title, rationale, proposed_change, "
        "agent_role. Do not use markdown fences or commentary. Keep all "
        "changes paper-only and do not request credentials, broker/order "
        "writes, account capability changes, destructive actions, startup "
        "tasks, real notional increases, or installer commands."
    )


def _bounded_retry(
    callback: Callable[[str], Any],
    timeout_seconds: float,
) -> Tuple[bool, Any]:
    results: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            results.put((True, callback(schema_retry_prompt())))
        except Exception as exc:  # Provider failures become quarantine reasons.
            results.put((False, "%s: %s" % (type(exc).__name__, exc)))

    thread = threading.Thread(target=run, name="llm-schema-retry", daemon=True)
    thread.start()
    try:
        return results.get(timeout=max(0.01, float(timeout_seconds)))
    except queue.Empty:
        return False, "schema_retry_timeout"


@dataclass
class IngestionCounters:
    statuses: Counter = field(default_factory=Counter)
    downstream: Counter = field(default_factory=Counter)
    terminal_failures: Counter = field(default_factory=Counter)
    repair_outcomes: Counter = field(default_factory=Counter)
    retried: int = 0
    quarantined: int = 0
    deduplications: int = 0
    dispatched: int = 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "native": int(self.statuses.get("native_valid", 0)),
            "recovered": int(self.statuses.get("recovered_valid", 0)),
            "retried": self.retried,
            "quarantined": self.quarantined,
            "deduplications": self.deduplications,
            "dispatched": self.dispatched,
            "by_parse_status": dict(sorted(self.statuses.items())),
            "by_downstream_task_type": dict(sorted(self.downstream.items())),
            "terminal_failure_reasons": dict(sorted(self.terminal_failures.items())),
            "repair_outcomes": dict(sorted(self.repair_outcomes.items())),
        }


class RecommendationLedger:
    """Append-only recommendation-ID ledger for restart-safe deduplication."""

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path else None
        self._ids: Set[str] = set()
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            value = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if isinstance(value, Mapping) and value.get("recommendation_id"):
                            self._ids.add(str(value["recommendation_id"]))
            except OSError:
                pass

    def claim(self, recommendation_id: str) -> bool:
        with self._lock:
            if recommendation_id in self._ids:
                return False
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        _stable_json({"recommendation_id": recommendation_id}) + "\n"
                    )
                    handle.flush()
            self._ids.add(recommendation_id)
            return True


class RecommendationIngestor:
    def __init__(
        self,
        *,
        ledger_path: Optional[os.PathLike] = None,
        retry_timeout_seconds: float = DEFAULT_RETRY_TIMEOUT_SECONDS,
    ) -> None:
        self.ledger = RecommendationLedger(ledger_path)
        self.retry_timeout_seconds = retry_timeout_seconds
        self.counters = IngestionCounters()

    def ingest(
        self,
        response: Any,
        *,
        retry: Optional[Callable[[str], Any]] = None,
        agent_role: Optional[str] = None,
        model_metadata: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        parent_recommendation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = normalize_recommendation(
            response,
            agent_role=agent_role,
            model_metadata=model_metadata,
            provenance=provenance,
            parent_recommendation_id=parent_recommendation_id,
        )
        self.counters.statuses[result["parse_status"]] += 1

        if not result["accepted"] and retry is not None and result["parse_status"] in {
            "truncated_json",
            "invalid_schema",
            "empty_response",
        }:
            self.counters.retried += 1
            ok, retry_response = _bounded_retry(retry, self.retry_timeout_seconds)
            if ok:
                retried = normalize_recommendation(
                    retry_response,
                    agent_role=agent_role,
                    model_metadata=model_metadata,
                    provenance=provenance,
                    parent_recommendation_id=parent_recommendation_id,
                )
                self.counters.statuses[retried["parse_status"]] += 1
                retried["retry_count"] = 1
                retried["initial_parse_status"] = result["parse_status"]
                retried["initial_raw_digest"] = result["raw_digest"]
                result = retried
            else:
                result["retry_count"] = 1
                result["terminal_failure_reason"] = str(retry_response)

        if not result["accepted"]:
            self.counters.quarantined += 1
            self.counters.terminal_failures[
                result.get("terminal_failure_reason", result["parse_status"])
            ] += 1
            return result

        recommendation_id = result["recommendation_id"]
        if not self.ledger.claim(recommendation_id):
            self.counters.deduplications += 1
            duplicate = dict(result)
            duplicate.update(
                {
                    "accepted": False,
                    "quarantined": False,
                    "ingestion_status": "deduplicated",
                    "duplicate_of": recommendation_id,
                }
            )
            return duplicate
        result["ingestion_status"] = "accepted"
        return result

    def dispatch(
        self,
        result: Mapping[str, Any],
        dispatcher: Callable[[Mapping[str, Any], str], Any],
    ) -> Any:
        if not result.get("accepted") or result.get("quarantined"):
            return None
        task_type = str(result["downstream_task_type"])
        outcome = dispatcher(result, task_type)
        self.counters.dispatched += 1
        self.counters.downstream[task_type] += 1
        return outcome

    def audit(self) -> Dict[str, Any]:
        return self.counters.snapshot()


def consume_swarm_payload(
    payload: Any,
    *,
    ingestor: RecommendationIngestor,
    dispatcher: Callable[[Mapping[str, Any], str], Any],
    retry: Optional[Callable[[str], Any]] = None,
) -> List[Dict[str, Any]]:
    """Normalize and dispatch a swarm report without network side effects."""

    if isinstance(payload, Mapping) and isinstance(payload.get("recommendations"), list):
        items = payload["recommendations"]
        inherited_model = payload.get("model_metadata")
        inherited_provenance = payload.get("provenance")
    elif isinstance(payload, list):
        items = payload
        inherited_model = None
        inherited_provenance = None
    else:
        items = [payload]
        inherited_model = None
        inherited_provenance = None

    results: List[Dict[str, Any]] = []
    for item in items:
        role = item.get("agent_role") if isinstance(item, Mapping) else None
        result = ingestor.ingest(
            item,
            retry=retry,
            agent_role=role,
            model_metadata=inherited_model if isinstance(inherited_model, Mapping) else None,
            provenance=(
                inherited_provenance
                if isinstance(inherited_provenance, Mapping)
                else None
            ),
        )
        results.append(result)
        ingestor.dispatch(result, dispatcher)
    return results


def add_ingestion_audit(
    report: MutableMapping[str, Any],
    ingestor_or_audit: Any,
) -> MutableMapping[str, Any]:
    """Attach the same counters to self-improvement reports/state packets."""

    if isinstance(ingestor_or_audit, RecommendationIngestor):
        audit = ingestor_or_audit.audit()
    else:
        audit = dict(ingestor_or_audit or {})
    report["recommendation_ingestion"] = audit
    return report


def attempt_bounded_preflight_repair(
    recommendation: Mapping[str, Any],
    *,
    preflight: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    repair: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    counters: Optional[IngestionCounters] = None,
) -> Dict[str, Any]:
    """Run no more than one repair for known structured preflight failures."""

    initial = dict(preflight(recommendation))
    if initial.get("ok"):
        if counters:
            counters.repair_outcomes["not_needed"] += 1
        return {
            "ok": True,
            "repair_attempted": False,
            "preflight": initial,
            "terminal_failure_reason": None,
        }

    reason = str(
        initial.get("failure_reason")
        or initial.get("status")
        or initial.get("error_code")
        or "preflight_failed"
    )
    if reason not in REPAIRABLE_PREFLIGHT_FAILURES:
        if counters:
            counters.repair_outcomes["not_repairable"] += 1
        return {
            "ok": False,
            "repair_attempted": False,
            "preflight": initial,
            "terminal_failure_reason": reason,
        }

    repaired = repair(recommendation, initial)
    second = dict(preflight(repaired))
    if second.get("ok"):
        if counters:
            counters.repair_outcomes["repaired"] += 1
        return {
            "ok": True,
            "repair_attempted": True,
            "repair_count": 1,
            "recommendation": repaired,
            "preflight": second,
            "initial_failure_reason": reason,
            "terminal_failure_reason": None,
        }

    terminal = str(
        second.get("failure_reason")
        or second.get("status")
        or second.get("error_code")
        or reason
    )
    if counters:
        counters.repair_outcomes["failed"] += 1
    return {
        "ok": False,
        "repair_attempted": True,
        "repair_count": 1,
        "recommendation": repaired,
        "preflight": second,
        "initial_failure_reason": reason,
        "terminal_failure_reason": terminal,
    }


# Compatibility aliases for callers that use parser-oriented terminology.
parse_recommendation = normalize_recommendation
parse_and_validate_recommendation = normalize_recommendation
