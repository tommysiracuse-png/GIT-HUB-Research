"""Shared scanner output contract for candidates and complete price observations."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable


@dataclasses.dataclass
class ScanBatch:
    source: str
    candidates: list[dict]
    observations: list[dict]
    generated_at: str = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat()
    )
    metadata: dict = dataclasses.field(default_factory=dict)


def observation_from_candidate(candidate: dict, source: str | None = None) -> dict:
    observed_at = (
        candidate.get("observed_at")
        or candidate.get("seen_at")
        or dt.datetime.now(dt.timezone.utc).isoformat()
    )
    return {
        "inst_id": candidate.get("inst_id"),
        "venue": candidate.get("venue"),
        "trade_type": candidate.get("trade_type"),
        "last": candidate.get("last"),
        "observed_at": observed_at,
        "price_source": source
        or (candidate.get("data_source") or {}).get("provider")
        or candidate.get("venue")
        or "scanner",
        "candidate": candidate,
    }


def normalize_observation(observation: dict, source: str | None = None) -> dict:
    if "inst_id" in observation and "observed_at" in observation:
        output = dict(observation)
        output.setdefault("price_source", source or output.get("venue") or "scanner")
        return output
    candidate = dict(observation)
    if "inst_id" not in candidate and candidate.get("instrument_id"):
        candidate["inst_id"] = candidate["instrument_id"]
    if "seen_at" not in candidate and candidate.get("last_checked_at"):
        candidate["seen_at"] = candidate["last_checked_at"]
    return observation_from_candidate(candidate, source=source)


def merge_observations(batches: Iterable[ScanBatch]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for batch in batches:
        for raw in batch.observations:
            observation = normalize_observation(raw, source=batch.source)
            inst_id = observation.get("inst_id")
            price = observation.get("last")
            if not inst_id or price in (None, ""):
                continue
            previous = merged.get(str(inst_id))
            if previous is None or str(observation.get("observed_at") or "") >= str(
                previous.get("observed_at") or ""
            ):
                merged[str(inst_id)] = observation
    return merged
