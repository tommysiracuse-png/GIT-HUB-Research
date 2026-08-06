"""Paper-only recommendation schema guard.

Provides a deterministic fallback recommendation object for downstream
parsers when a model response is incomplete or invalid. This is strictly for
simulation and reporting; it does not enable execution.
"""

from __future__ import annotations

import json
from typing import Any


REQUIRED_RECOMMENDATION_KEYS = (
    "action",
    "priority",
    "title",
    "rationale",
    "market_key",
    "evidence",
    "proposed_change",
)

CROSS_MARKET_RESEARCHER_ALLOWED_ACTIONS = frozenset(
    {"no_action", "propose_diagnostic_hypothesis"}
)
RED_TEAM_ALLOWED_ACTIONS = frozenset({"no_action", "propose_diagnostic_hypothesis"})


def _reject_non_json_constant(value: str) -> None:
    """Reject JavaScript-style numeric constants that are not valid JSON."""
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object only when every key appears once.

    Python's standard decoder otherwise keeps the final duplicate value, which
    makes a recommendation ambiguous to consumers that use another parser.
    """
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def paper_only_fallback_recommendation(
    *,
    market_key: str = "paper.market_radar.fallback",
    title: str = "Paper-only fallback: no trade",
    rationale: str = "Signal generation failed schema validation; maintain observation mode.",
) -> dict[str, Any]:
    return {
        "action": "hold",
        "priority": "medium",
        "title": title,
        "rationale": rationale,
        "market_key": market_key,
        "evidence": {
            "issue_type": "schema_validation_failure",
            "paper_only_status": "no live trading",
        },
        "proposed_change": {
            "summary": "Return safe default recommendation and log validation error.",
            "paper_trade_instruction": "Simulation only; no execution.",
        },
    }


def paper_only_no_action_fallback(
    *,
    market_key: str = "paper.market_radar.schema_guard",
    title: str = "Paper-only schema guard: no action",
    rationale: str = "The generated recommendation was incomplete or invalid after processing.",
) -> dict[str, Any]:
    """Return the minimal schema-valid paper-only terminal recommendation.

    This object deliberately uses ``no_action`` rather than attempting to
    infer a strategy, trade, or implementation change from malformed model
    output.  Consumers may log it, but it is not actionable.
    """
    return {
        "action": "no_action",
        "priority": 1,
        "title": title,
        "rationale": rationale,
        "market_key": market_key,
        "evidence": {
            "issue_type": "schema_validation_failure",
            "paper_only_status": "no live trading",
        },
        "proposed_change": {
            "summary": "Do not act on invalid model output; retain the failure for paper-only diagnostics.",
            "paper_trade_instruction": "No action. Simulation and reporting only; no execution.",
        },
    }


def validate_recommendation_object(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(key in payload for key in REQUIRED_RECOMMENDATION_KEYS)


def validate_cross_market_researcher_object(payload: Any) -> None:
    """Raise when a cross-market recommendation violates its runtime contract."""
    if not isinstance(payload, dict):
        raise ValueError("cross-market recommendation must be a JSON object")
    missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in payload]
    if missing:
        raise ValueError(
            "cross-market recommendation missing required fields: " + ", ".join(missing)
        )
    unexpected = [key for key in payload if key not in REQUIRED_RECOMMENDATION_KEYS]
    if unexpected:
        raise ValueError(
            "cross-market recommendation contains unexpected fields: "
            + ", ".join(sorted(unexpected))
        )
    if payload["action"] not in CROSS_MARKET_RESEARCHER_ALLOWED_ACTIONS:
        raise ValueError("cross-market recommendation action is not allowed")
    for field in ("title", "rationale", "market_key"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"cross-market recommendation {field} must be a non-empty string"
            )
    # bool is an int subclass, but it is not a meaningful recommendation priority.
    if isinstance(payload["priority"], bool) or not isinstance(payload["priority"], int):
        raise ValueError("cross-market recommendation priority must be an integer")
    if not isinstance(payload["evidence"], dict):
        raise ValueError("cross-market recommendation evidence must be a JSON object")
    if not payload["evidence"]:
        raise ValueError("cross-market recommendation evidence must not be empty")
    if not isinstance(payload["proposed_change"], dict):
        raise ValueError("cross-market recommendation proposed_change must be a JSON object")
    if not payload["proposed_change"]:
        raise ValueError("cross-market recommendation proposed_change must not be empty")


def validate_red_team_object(payload: Any) -> None:
    """Raise when a red-team recommendation violates the strict output contract."""
    if not isinstance(payload, dict):
        raise ValueError("red-team recommendation must be a JSON object")
    missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in payload]
    if missing:
        raise ValueError(
            "red-team recommendation missing required fields: " + ", ".join(missing)
        )
    unexpected = [key for key in payload if key not in REQUIRED_RECOMMENDATION_KEYS]
    if unexpected:
        raise ValueError(
            "red-team recommendation contains unexpected fields: "
            + ", ".join(sorted(unexpected))
        )
    if payload["action"] not in RED_TEAM_ALLOWED_ACTIONS:
        raise ValueError("red-team recommendation action is not allowed")
    if isinstance(payload["priority"], bool) or not isinstance(payload["priority"], int):
        raise ValueError("red-team recommendation priority must be an integer")
    if not isinstance(payload["evidence"], dict):
        raise ValueError("red-team recommendation evidence must be a JSON object")
    if not isinstance(payload["proposed_change"], dict):
        raise ValueError("red-team recommendation proposed_change must be a JSON object")


def finalize_cross_market_researcher_response(response: str | bytes | bytearray) -> dict[str, Any]:
    """Parse and validate one complete cross-market researcher response."""
    payload = finalize_recommendation_response(response)
    validate_cross_market_researcher_object(payload)
    return payload


def finalize_red_team_response(response: str | bytes | bytearray) -> dict[str, Any]:
    """Parse and validate one complete red-team response."""
    payload = finalize_recommendation_response(response)
    validate_red_team_object(payload)
    return payload


def cross_market_researcher_schema_fallback(
    validation_error: str,
    *,
    raw_generation_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a paper-only diagnostic recommendation after schema validation fails."""
    return {
        "action": "propose_diagnostic_hypothesis",
        "priority": 100,
        "title": "Cross-market researcher response schema violation",
        "rationale": (
            "The generated cross-market recommendation was not schema-valid or lacked "
            "enough evidence for a supported thesis; retain the failure as a paper-only "
            "diagnostic rather than using it for execution."
        ),
        "market_key": "paper.cross_market_researcher.schema_fallback",
        "evidence": {
            "schema_violation": validation_error,
            "raw_generation_metadata": raw_generation_metadata,
            "insufficient_market_evidence_defaults_to_diagnostic": True,
            "paper_only": True,
        },
        "proposed_change": {
            "summary": "Repair the cross-market response schema and rerun the paper-only analysis.",
            "fallback_mode": "paper_only_diagnostic_hypothesis",
            "paper_only": True,
            "live_trading": "disabled",
        },
    }


def red_team_schema_fallback(
    validation_error: str,
    *,
    raw_generation_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a paper-only diagnostic recommendation after red-team schema failure."""
    return {
        "action": "propose_diagnostic_hypothesis",
        "priority": 100,
        "title": "Red-team response schema violation",
        "rationale": (
            "The generated red-team recommendation was not schema-valid, so the "
            "response was converted into a paper-only diagnostic record."
        ),
        "market_key": "paper.red_team.schema_fallback",
        "evidence": {
            "schema_violation": validation_error,
            "raw_generation_metadata": raw_generation_metadata,
            "paper_only": True,
        },
        "proposed_change": {
            "summary": "Repair the red-team response schema and rerun the paper-only diagnosis.",
            "paper_only": True,
            "live_trading": "disabled",
        },
    }


def finalize_recommendation_response(response: str | bytes | bytearray) -> dict[str, Any]:
    """Return one complete recommendation object or reject the model response.

    The decoder consumes the entire response, so markdown fences, commentary,
    multiple values, arrays, and partial objects are never recovered
    heuristically. Non-standard constants and duplicate object keys are also
    rejected so all consumers see the same recommendation. Whitespace around
    the single JSON value is permitted.
    """
    if not isinstance(response, (str, bytes, bytearray)):
        raise ValueError("recommendation response must be JSON text")
    try:
        payload = json.loads(
            response,
            parse_constant=_reject_non_json_constant,
            object_pairs_hook=_unique_object_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("recommendation response must be exactly one complete JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("recommendation response must be a JSON object")
    missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in payload]
    if missing:
        raise ValueError(f"recommendation response missing required fields: {', '.join(missing)}")
    return payload
