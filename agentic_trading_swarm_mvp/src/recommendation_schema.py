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


def validate_recommendation_object(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(key in payload for key in REQUIRED_RECOMMENDATION_KEYS)


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
