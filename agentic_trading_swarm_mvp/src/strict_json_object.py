#!/usr/bin/env python3
"""Strict single-object JSON helpers for paper-only machine parsing."""

from __future__ import annotations

import json
from typing import Iterable


def _as_json_object(value: object) -> dict | None:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            parsed = json.loads(value.decode("utf-8"))
        else:
            parsed = json.loads(str(value))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _missing_required_keys(payload: dict, required_keys: Iterable[str]) -> list[str]:
    return [str(key) for key in required_keys if str(key) not in payload]


def parse_single_json_object(value: object, *, required_keys: Iterable[str] = ()) -> dict | None:
    """Parse exactly one JSON object and require selected top-level keys."""
    payload = _as_json_object(value)
    if payload is None:
        return None
    if _missing_required_keys(payload, required_keys):
        return None
    return payload


def coerce_single_json_object(
    value: object,
    *,
    required_keys: Iterable[str] = (),
    default: dict | None = None,
) -> dict:
    """Return a validated JSON object or a safe default."""
    payload = parse_single_json_object(value, required_keys=required_keys)
    if payload is not None:
        return payload
    if default is None:
        return {}
    return dict(default)


def validate_single_json_object(
    value: object,
    *,
    required_keys: Iterable[str] = (),
    context: str = "json object",
) -> dict:
    """Validate that a value is one complete JSON object with required keys."""
    payload = _as_json_object(value)
    if payload is None:
        raise ValueError(f"{context} must be exactly one complete JSON object")
    missing = _missing_required_keys(payload, required_keys)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"{context} missing required top-level keys: {missing_list}")
    return payload


__all__ = ["coerce_single_json_object", "parse_single_json_object", "validate_single_json_object"]
