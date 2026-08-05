"""Runtime execution and reporting for registered public-market adapters."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import pathlib
from collections import Counter
from typing import Any

from adapters.registry import discover_adapters, get_adapter
from scan_batch import ScanBatch
from storage import RUNS_DIR


REPORT_JSON = RUNS_DIR / "public_market_adapters_latest.json"
REPORT_MD = RUNS_DIR / "public_market_adapters_report.md"
CACHE_DIR = RUNS_DIR / "public_adapter_cache"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _adapter_config(settings: dict, adapter_id: str) -> dict:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _cache_path(adapter_id: str) -> pathlib.Path:
    return CACHE_DIR / f"{adapter_id}.json"


def _batch_payload(batch: ScanBatch) -> dict:
    return {
        "source": batch.source,
        "candidates": batch.candidates,
        "observations": batch.observations,
        "generated_at": batch.generated_at,
        "metadata": batch.metadata,
    }


def _batch_from_payload(payload: dict) -> ScanBatch:
    return ScanBatch(
        source=str(payload.get("source") or "public_market_adapter_cache"),
        candidates=list(payload.get("candidates") or []),
        observations=list(payload.get("observations") or []),
        generated_at=str(payload.get("generated_at") or _utc_now()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _load_fresh_cache(adapter_id: str, cache_minutes: int) -> ScanBatch | None:
    path = _cache_path(adapter_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    generated = _parse_time(payload.get("cached_at"))
    if not generated:
        return None
    age = (dt.datetime.now(dt.timezone.utc) - generated).total_seconds()
    if age > max(0, int(cache_minutes)) * 60:
        return None
    batch = _batch_from_payload(dict(payload.get("batch") or {}))
    batch.metadata = {**batch.metadata, "cache_status": "fresh_cache", "cache_age_seconds": round(age, 3)}
    return batch


def _write_cache(adapter_id: str, batch: ScanBatch) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(adapter_id).write_text(
        json.dumps({"cached_at": _utc_now(), "batch": _batch_payload(batch)}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _run_adapter(adapter_id: str, settings: dict) -> tuple[str, ScanBatch, str]:
    adapter = get_adapter(adapter_id)
    info = adapter.info
    cfg = _adapter_config(settings, adapter_id)
    cache_minutes = int(cfg.get("cache_minutes", info.default_cache_minutes))
    cached = _load_fresh_cache(adapter_id, cache_minutes)
    if cached:
        return adapter_id, cached, "cache"
    result = adapter.scan(settings)
    if isinstance(result, ScanBatch):
        batch = result
    else:
        batch = ScanBatch(source=info.source, candidates=[], observations=list(result or []))
    batch.metadata = {**batch.metadata, "adapter_id": adapter_id, "cache_status": "refreshed"}
    _write_cache(adapter_id, batch)
    return adapter_id, batch, "network"


def _write_report(report: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Public Market Adapter Runtime",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Adapters: `{report['summary']['adapter_count']}`",
        f"Observations: `{report['summary']['observation_count']}`",
        f"Candidates: `{report['summary']['candidate_count']}`",
        f"Source status: `{report['summary']['by_source_status']}`",
        "",
        "## Adapters",
        "",
        "| Adapter | Venue | Status | Cache | Observations | Candidates | Gap |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for item in report["adapters"]:
        lines.append(
            f"| `{item['adapter_id']}` | `{item['venue']}` | `{item['source_status']}` | "
            f"`{item['cache_status']}` | {item['observation_count']} | {item['candidate_count']} | "
            f"{item.get('capability_gap') or ''} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_scan_batch(settings: dict) -> ScanBatch:
    cfg = settings.get("public_market_adapters") or {}
    if not cfg.get("enabled", True):
        return ScanBatch(source="public_market_adapter_plugins", candidates=[], observations=[], metadata={"enabled": False})
    adapter_ids = [
        adapter_id
        for adapter_id in discover_adapters()
        if getattr(get_adapter(adapter_id).info, "active", True)
        and _adapter_config(settings, adapter_id).get("enabled", True)
    ]
    workers = max(1, min(int(cfg.get("workers", 5)), len(adapter_ids) or 1))
    completed: list[tuple[str, ScanBatch, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_adapter, adapter_id, settings): adapter_id for adapter_id in adapter_ids}
        for future in concurrent.futures.as_completed(futures):
            adapter_id = futures[future]
            try:
                completed.append(future.result())
            except Exception as exc:  # noqa: BLE001 - one public source must not stop radar.
                info = get_adapter(adapter_id).info
                completed.append(
                    (
                        adapter_id,
                        ScanBatch(
                            source=info.source,
                            candidates=[],
                            observations=[],
                            metadata={"adapter_id": adapter_id, "source_status": "runtime_error", "error": str(exc)[:300]},
                        ),
                        "error",
                    )
                )
    completed.sort(key=lambda item: item[0])
    candidates: list[dict] = []
    observations: list[dict] = []
    normalized_batches: dict[str, tuple[list[dict], list[dict]]] = {}
    for adapter_id, batch, _mode in completed:
        info = get_adapter(adapter_id).info
        batch_candidates = []
        batch_observations = []
        for candidate in batch.candidates:
            normalized = dict(candidate)
            normalized.setdefault("adapter_id", adapter_id)
            normalized.setdefault("source_adapter_id", adapter_id)
            normalized.setdefault("venue", info.venue)
            batch_candidates.append(normalized)
        for observation in batch.observations:
            normalized = dict(observation)
            normalized.setdefault("adapter_id", adapter_id)
            normalized.setdefault("source_adapter_id", adapter_id)
            normalized.setdefault("venue", info.venue)
            batch_observations.append(normalized)
        normalized_batches[adapter_id] = (batch_candidates, batch_observations)
        candidates.extend(batch_candidates)
        observations.extend(batch_observations)
    details = []
    statuses = Counter()
    venues = Counter()
    surfaces = Counter()
    for adapter_id, batch, mode in completed:
        info = get_adapter(adapter_id).info
        batch_candidates, batch_observations = normalized_batches[adapter_id]
        source_status = str(batch.metadata.get("source_status") or "unknown")
        statuses[source_status] += 1
        venues[str(info.venue)] += len(batch_observations)
        batch_surfaces = Counter(
            str(row.get("market_surface") or row.get("market_type") or "unknown")
            for row in batch_observations
        )
        surfaces.update(batch_surfaces)
        available_fields = sorted(
            {
                str(key)
                for row in batch_observations[:100]
                for key, value in row.items()
                if value not in (None, "", [], {})
            }
        )
        details.append(
            {
                "adapter_id": adapter_id,
                "venue": info.venue,
                "market_type": info.market_type,
                "source_status": source_status,
                "cache_status": batch.metadata.get("cache_status") or mode,
                "observation_count": len(batch_observations),
                "price_observation_count": sum(float(row.get("last") or 0.0) > 0.0 for row in batch_observations),
                "candidate_count": len(batch_candidates),
                "research_only_count": sum(
                    1
                    for row in batch_observations
                    if row.get("direction") == "watch_only" or row.get("candidate_reject_reason")
                ),
                "market_surfaces": dict(batch_surfaces),
                "sample_instruments": [
                    str(row.get("inst_id") or row.get("instrument_id"))
                    for row in batch_observations
                    if row.get("inst_id") or row.get("instrument_id")
                ][:8],
                "available_fields": available_fields,
                "capability_gap": batch.metadata.get("capability_gap"),
                "adapter_spec_id": batch.metadata.get("adapter_spec_id"),
                "paper_only": bool(batch.metadata.get("paper_only", True)),
                "runtime_entrypoint": info.runtime_entrypoint,
                "docs_url": info.docs_url,
            }
        )
    report = {
        "generated_at": _utc_now(),
        "summary": {
            "adapter_count": len(completed),
            "observation_count": len(observations),
            "candidate_count": len(candidates),
            "by_source_status": dict(statuses),
            "observations_by_venue": dict(venues),
            "observations_by_market_surface": dict(surfaces),
            "surface_inventory": [
                {
                    "adapter_id": item["adapter_id"],
                    "venue": item["venue"],
                    "market_surfaces": item["market_surfaces"],
                    "sample_instruments": item["sample_instruments"][:4],
                    "candidate_count": item["candidate_count"],
                }
                for item in details
            ],
        },
        "adapters": details,
    }
    _write_report(report)
    return ScanBatch(
        source="public_market_adapter_plugins",
        candidates=candidates,
        observations=observations,
        metadata={"enabled": True, "public_market_adapters": report},
    )
