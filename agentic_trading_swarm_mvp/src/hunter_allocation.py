"""Exploit/explore/diagnose allocation from hunter directives."""

from __future__ import annotations

import collections
from typing import Any


DEFAULT_BUCKETS = {"exploit": 0.5, "explore": 0.3, "diagnose": 0.2}


def classify_directive(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key, "")) for key in ("directive", "market_key", "rationale")).lower()
    if any(token in text for token in ("exploit", "promote", "positive", "expand")):
        return "exploit"
    if any(token in text for token in ("diagnose", "decay", "red-team", "failure", "weak")):
        return "diagnose"
    return "explore"


def allocate_review_slots(directives: list[dict[str, Any]], total_slots: int, buckets: dict[str, float] | None = None) -> dict[str, Any]:
    buckets = buckets or DEFAULT_BUCKETS
    total_slots = max(0, int(total_slots))
    raw = {name: int(total_slots * share) for name, share in buckets.items()}
    remainder = total_slots - sum(raw.values())
    for name in ("exploit", "explore", "diagnose"):
        if remainder <= 0:
            break
        raw[name] = raw.get(name, 0) + 1
        remainder -= 1
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in directives:
        grouped[classify_directive(item)].append(item)
    return {
        "total_slots": total_slots,
        "slot_targets": raw,
        "directive_counts": {name: len(grouped.get(name, [])) for name in ("exploit", "explore", "diagnose")},
        "selected_directives": {
            name: sorted(grouped.get(name, []), key=lambda row: int(row.get("priority") or 0), reverse=True)[: raw.get(name, 0)]
            for name in ("exploit", "explore", "diagnose")
        },
    }


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key, ""))
        for key in ("venue", "inst_id", "direction", "trade_type", "signal_key", "market_key", "route_status")
    ).lower()


def _matches_directive(candidate: dict[str, Any], directive: dict[str, Any]) -> bool:
    target = str(directive.get("market_key") or directive.get("signal_key") or "").lower()
    if not target:
        return False
    return any(part and part in _candidate_text(candidate) for part in target.replace("|", " ").split())


def allocate_candidate_review(
    candidates: list[dict[str, Any]],
    directives: list[dict[str, Any]],
    total_slots: int,
    buckets: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    allocation = allocate_review_slots(directives, total_slots, buckets=buckets)
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    by_bucket: dict[str, int] = {"exploit": 0, "explore": 0, "diagnose": 0, "fallback": 0}
    for bucket, bucket_directives in allocation["selected_directives"].items():
        target = allocation["slot_targets"].get(bucket, 0)
        bucket_selected = 0
        for candidate in candidates:
            if bucket_selected >= target:
                break
            if id(candidate) in selected_ids:
                continue
            if any(_matches_directive(candidate, directive) for directive in bucket_directives):
                row = dict(candidate)
                row["_hunter_bucket"] = bucket
                selected.append(row)
                selected_ids.add(id(candidate))
                by_bucket[bucket] += 1
                bucket_selected += 1
    for candidate in candidates:
        if len(selected) >= total_slots:
            break
        if id(candidate) in selected_ids:
            continue
        row = dict(candidate)
        row["_hunter_bucket"] = "fallback"
        selected.append(row)
        selected_ids.add(id(candidate))
        by_bucket["fallback"] += 1
    report = {
        **allocation,
        "selected_count": len(selected),
        "selected_by_bucket": by_bucket,
        "minimum_exploration_floor": allocation["slot_targets"].get("explore", 0),
    }
    return selected, report
