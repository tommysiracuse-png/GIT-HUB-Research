"""Exploit/explore/diagnose allocation from hunter directives."""

from __future__ import annotations

import collections
import datetime as dt
import json
from typing import Any

from storage import RUNS_DIR


DEFAULT_BUCKETS = {"exploit": 0.5, "explore": 0.3, "diagnose": 0.2}
GLOBAL_DISCOVERY_MIN_EXPLORE_SLOTS = 4
REPORT_JSON = RUNS_DIR / "hunter_allocation_report.json"
REPORT_MD = RUNS_DIR / "hunter_allocation_report.md"
DISCOVERY_JSONL = RUNS_DIR / "market_discovery_candidates.jsonl"


def classify_directive(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key, "")) for key in ("directive", "market_key", "rationale")).lower()
    if any(token in text for token in ("exploit", "promote", "positive", "expand")):
        return "exploit"
    if any(token in text for token in ("diagnose", "decay", "red-team", "failure", "weak")):
        return "diagnose"
    if any(token in text for token in ("global_market_discovery", "discover", "research", "new market", "new venue")):
        return "explore"
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
    if target.startswith("global_discovery|"):
        target = target.split("|", 1)[1]
    return any(part and part in _candidate_text(candidate) for part in target.replace("|", " ").split())


def _best_matching_directive(candidate: dict[str, Any], directives: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [directive for directive in directives if _matches_directive(candidate, directive)]
    if not matches:
        return None
    return sorted(matches, key=lambda row: int(row.get("priority") or 0), reverse=True)[0]


def _is_global_discovery_candidate(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("market_surface") == "global_market_discovery"
        or candidate.get("trade_type") == "global_market_discovery_proxy"
        or str(candidate.get("market_key") or "").lower().startswith("global_discovery|")
    )


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
        if bucket == "explore" and target > 0:
            floor = min(target, GLOBAL_DISCOVERY_MIN_EXPLORE_SLOTS)
            for candidate in candidates:
                if bucket_selected >= floor:
                    break
                if id(candidate) in selected_ids:
                    continue
                if not _is_global_discovery_candidate(candidate):
                    continue
                row = dict(candidate)
                row["_hunter_bucket"] = "explore"
                row["_hunter_directive_id"] = None
                row["_hunter_allocation_reason"] = "global_discovery_exploration_floor"
                selected.append(row)
                selected_ids.add(id(candidate))
                by_bucket["explore"] += 1
                bucket_selected += 1
        for candidate in candidates:
            if bucket_selected >= target:
                break
            if id(candidate) in selected_ids:
                continue
            directive = _best_matching_directive(candidate, bucket_directives)
            if directive:
                row = dict(candidate)
                row["_hunter_bucket"] = bucket
                row["_hunter_directive_id"] = directive.get("id")
                row["_hunter_allocation_reason"] = directive.get("rationale") or directive.get("directive")
                selected.append(row)
                selected_ids.add(id(candidate))
                by_bucket[bucket] += 1
                bucket_selected += 1
        if bucket == "explore" and bucket_selected < target:
            for candidate in candidates:
                if bucket_selected >= target:
                    break
                if id(candidate) in selected_ids:
                    continue
                if not _is_global_discovery_candidate(candidate):
                    continue
                row = dict(candidate)
                row["_hunter_bucket"] = "explore"
                row["_hunter_directive_id"] = None
                row["_hunter_allocation_reason"] = "global_discovery_exploration_floor"
                selected.append(row)
                selected_ids.add(id(candidate))
                by_bucket["explore"] += 1
                bucket_selected += 1
    for candidate in candidates:
        if len(selected) >= total_slots:
            break
        if id(candidate) in selected_ids:
            continue
        row = dict(candidate)
        row["_hunter_bucket"] = "fallback"
        row["_hunter_directive_id"] = None
        row["_hunter_allocation_reason"] = "fallback_best_remaining_candidate"
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


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _global_discovery_counts() -> dict[str, Any]:
    if not DISCOVERY_JSONL.exists():
        return {"total": 0, "last_hour": 0, "last_day": 0, "by_region": {}, "by_surface_type": {}}
    now = dt.datetime.now(dt.timezone.utc)
    total = 0
    last_hour = 0
    last_day = 0
    by_region: dict[str, int] = {}
    by_surface: dict[str, int] = {}
    for line in DISCOVERY_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        by_region[str(item.get("region") or "unknown")] = by_region.get(str(item.get("region") or "unknown"), 0) + 1
        surface = str(item.get("surface_type_classified") or "unknown")
        by_surface[surface] = by_surface.get(surface, 0) + 1
        created = _parse_iso(item.get("created_at"))
        if created:
            age = now - created
            if age <= dt.timedelta(hours=1):
                last_hour += 1
            if age <= dt.timedelta(days=1):
                last_day += 1
    return {
        "total": total,
        "last_hour": last_hour,
        "last_day": last_day,
        "by_region": by_region,
        "by_surface_type": by_surface,
    }


def write_hunter_allocation_report(
    allocation: dict[str, Any],
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = (settings or {}).get("hunter_allocation", {})
    by_bucket_candidates: dict[str, int] = {"exploit": 0, "explore": 0, "diagnose": 0, "fallback": 0}
    selected_markets = []
    for row in selected:
        bucket = str(row.get("_hunter_bucket") or "fallback")
        by_bucket_candidates[bucket] = by_bucket_candidates.get(bucket, 0) + 1
        selected_markets.append(
            {
                "bucket": bucket,
                "venue": row.get("venue"),
                "inst_id": row.get("inst_id"),
                "direction": row.get("direction"),
                "trade_type": row.get("trade_type"),
                "signal_key": row.get("signal_key"),
                "reason": row.get("_hunter_allocation_reason"),
            }
        )
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "enabled": bool(cfg.get("enabled", True)),
        "apply_to_candidate_review": bool(cfg.get("apply_to_candidate_review", True)),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "slot_targets": allocation.get("slot_targets", {}),
        "directive_counts": allocation.get("directive_counts", {}),
        "selected_by_bucket": by_bucket_candidates,
        "minimum_exploration_floor": allocation.get("minimum_exploration_floor", 0),
        "fallback_count": by_bucket_candidates.get("fallback", 0),
        "global_discovery": _global_discovery_counts(),
        "selected_markets": selected_markets[:100],
        "reports": {
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
            "global_discovery_ledger": str(DISCOVERY_JSONL),
        },
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    REPORT_MD.write_text(_report_markdown(report), encoding="utf-8")
    return report


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Hunter Allocation Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Applies to candidate review: `{report.get('apply_to_candidate_review')}`",
        f"- Candidates seen: `{report.get('candidate_count')}`",
        f"- Selected for review: `{report.get('selected_count')}`",
        f"- Slot targets: `{report.get('slot_targets', {})}`",
        f"- Selected by bucket: `{report.get('selected_by_bucket', {})}`",
        f"- Global discoveries: `{report.get('global_discovery', {})}`",
        "",
        "## Selected Markets",
        "",
    ]
    for item in report.get("selected_markets", [])[:30]:
        lines.append(
            f"- `{item.get('bucket')}` `{item.get('inst_id')}` `{item.get('direction')}` "
            f"`{item.get('trade_type')}` reason={item.get('reason')}"
        )
    return "\n".join(lines) + "\n"
