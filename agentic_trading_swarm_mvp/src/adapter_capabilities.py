"""Capability inventory and adapter-spec reconciliation.

Discovery ideas, runtime adapters, and remaining data gaps are deliberately
separate concepts.  This module gives the research worker and reports one
canonical inventory so an implemented parser is not requested again, while a
missing order book is not falsely treated as a complete venue adapter.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import urllib.parse
from collections import Counter
from typing import Any

from adapters.registry import adapter_records
from frontier_crypto_adapter import load_venue_registry
from storage import RUNS_DIR


REPORT_JSON = RUNS_DIR / "adapter_capability_inventory.json"
REPORT_MD = RUNS_DIR / "adapter_capability_inventory.md"

IDENTITY_STOPWORDS = {
    "adapter",
    "api",
    "data",
    "derivative",
    "derivatives",
    "exchange",
    "market",
    "markets",
    "official",
    "public",
    "source",
    "stock",
    "venue",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _words(value: Any) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split()
        if len(token) > 1 and token not in IDENTITY_STOPWORDS
    }


def _host(value: Any) -> str:
    try:
        return (urllib.parse.urlparse(str(value or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _normalized_url(value: Any) -> str:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def _legacy_records() -> list[dict]:
    records = []
    for row in load_venue_registry().get("venues", []):
        if not isinstance(row, dict) or not row.get("venue"):
            continue
        capabilities = {"public_market_data", "ticker"}
        if row.get("static_status") == "watch_only":
            capabilities = {"catalog", "watch_only"}
        if isinstance(row.get("depth"), dict):
            capabilities.update({"order_book", "executable_quality"})
        records.append(
            {
                "adapter_id": str(row.get("route_id") or f"{row['venue']}_legacy").lower(),
                "venue": str(row["venue"]),
                "market_type": str(row.get("market_type") or "unknown"),
                "source": str(row.get("url") or "frontier venue registry"),
                "capabilities": sorted(capabilities),
                "aliases": sorted(
                    {
                        str(row["venue"]),
                        str(row.get("route_id") or ""),
                        str(row.get("parser") or ""),
                    }
                    - {""}
                ),
                "docs_url": row.get("url"),
                "runtime_entrypoint": "frontier_crypto_adapter.build_scan_batch",
                "quote_assets": [],
                "active": bool(row.get("enabled", True)),
                "implementation": "legacy_frontier_registry",
            }
        )
    return records


def capability_inventory() -> list[dict]:
    by_id: dict[str, dict] = {}
    for record in [*_legacy_records(), *adapter_records()]:
        by_id[str(record["adapter_id"])] = record
    return sorted(by_id.values(), key=lambda item: (str(item.get("venue")), str(item.get("adapter_id"))))


def _required_capabilities(text: str, spec: dict) -> set[str]:
    blob = f"{text} {json.dumps(spec, sort_keys=True, default=str)}".lower()
    required: set[str] = set()
    if any(token in blob for token in ("order book", "orderbook", "market depth", "level 2", "depth")):
        required.add("order_book")
    if any(
        token in blob
        for token in (
            "real-time",
            "real time",
            "realtime",
            "intraday",
            "five-second",
            "five second",
            "5-second",
            "5 second",
            "streaming quote",
            "live quote",
            "websocket",
        )
    ):
        required.add("entry_quality_quote")
    if any(token in blob for token in ("ticker", "quote", "price", "market data", "ohlc")):
        required.add("ticker")
    if any(token in blob for token in ("catalog", "instrument list", "contract list", "cross-list")):
        required.add("catalog")
    if any(token in blob for token in ("settlement", "closing price", "daily bar", "daily ohlc")):
        required.add("settlement_reference")
    return required or {"public_market_data"}


def _spec_identity(spec_row: dict) -> dict:
    spec = spec_row.get("spec") if isinstance(spec_row.get("spec"), dict) else _json(spec_row.get("spec_json"))
    candidate = spec.get("candidate") if isinstance(spec.get("candidate"), dict) else {}
    title = str(spec_row.get("title") or "")
    market_key = str(spec_row.get("market_key") or "")
    venue = str(
        candidate.get("venue_or_source")
        or candidate.get("venue")
        or spec.get("venue_or_source")
        or spec.get("venue")
        or ""
    ).strip()
    urls = [
        candidate.get("public_docs_url"),
        *(candidate.get("source_urls") or []),
        spec.get("public_docs_url"),
        *(spec.get("source_urls") or []),
    ]
    text = " ".join([title, market_key, venue, json.dumps(spec, sort_keys=True, default=str)])
    return {
        "spec": spec,
        "venue": venue,
        "venue_words": _words(venue),
        "words": _words(text),
        "hosts": {_host(value) for value in urls if _host(value)},
        "urls": {_normalized_url(value) for value in urls if _normalized_url(value)},
        "required_capabilities": _required_capabilities(text, spec),
    }


def match_adapter_spec(spec_row: dict, inventory: list[dict] | None = None) -> dict:
    identity = _spec_identity(spec_row)
    scored = []
    for adapter in inventory or capability_inventory():
        adapter_words = _words(
            " ".join(
                [
                    str(adapter.get("adapter_id") or ""),
                    str(adapter.get("venue") or ""),
                    str(adapter.get("source") or ""),
                    " ".join(adapter.get("aliases") or []),
                ]
            )
        )
        hosts = {_host(adapter.get("docs_url")), _host(adapter.get("source"))} - {""}
        urls = {
            _normalized_url(adapter.get("docs_url")),
            _normalized_url(adapter.get("source")),
        } - {""}
        exact_url_match = bool(identity["urls"].intersection(urls))
        host_match = bool(identity["hosts"].intersection(hosts))
        venue_overlap = identity["venue_words"].intersection(adapter_words)
        exact_venue = bool(identity["venue_words"] and identity["venue_words"] <= adapter_words)
        score = (
            (100 if exact_url_match else 0)
            + (100 if host_match else 0)
            + (50 if exact_venue else 0)
            + min(20, len(venue_overlap) * 5)
        )
        if not host_match and not exact_venue:
            continue
        available = set(adapter.get("capabilities") or [])
        required = set(identity["required_capabilities"])
        # A concrete ticker capability satisfies the broad public-data request.
        if "public_market_data" in required and available.intersection(
            {"public_market_data", "ticker", "daily_ohlcv", "delayed_quote", "event_price_reference"}
        ):
            required.remove("public_market_data")
        satisfied = set(available)
        if available.intersection({"ticker", "ticker_reference", "daily_ohlcv", "delayed_quote", "event_price_reference"}):
            satisfied.add("ticker")
        if available.intersection({"settlement_reference", "daily_ohlcv", "event_price_reference"}):
            satisfied.add("settlement_reference")
        if available.intersection(
            {"ticker", "realtime_ticker", "live_quote", "intraday_quote", "order_book", "executable_quality"}
        ):
            satisfied.add("entry_quality_quote")
        missing = sorted(required - satisfied)
        scored.append((score, len(missing), adapter, missing))
    if not scored:
        return {
            "match_status": "no_runtime_adapter_match",
            "adapter_id": None,
            "required_capabilities": sorted(identity["required_capabilities"]),
            "missing_capabilities": sorted(identity["required_capabilities"]),
        }
    _score, _missing_count, adapter, missing = sorted(
        scored,
        key=lambda item: (-item[0], item[1], str(item[2].get("adapter_id"))),
    )[0]
    return {
        "match_status": "fully_covered" if not missing else "partial_capability_gap",
        "adapter_id": adapter.get("adapter_id"),
        "venue": adapter.get("venue"),
        "runtime_entrypoint": adapter.get("runtime_entrypoint"),
        "required_capabilities": sorted(identity["required_capabilities"]),
        "available_capabilities": sorted(adapter.get("capabilities") or []),
        "missing_capabilities": missing,
    }


def candidate_has_runtime_capability(candidate: dict, inventory: list[dict] | None = None) -> dict:
    synthetic = {
        "title": f"Adapter for {candidate.get('venue_or_source') or candidate.get('venue') or ''}",
        "market_key": f"global_discovery|{candidate.get('venue_or_source') or ''}",
        "spec": {"candidate": candidate},
    }
    return match_adapter_spec(synthetic, inventory=inventory)


def _canonical_key(spec_row: dict, match: dict) -> str:
    identity = _spec_identity(spec_row)
    adapter = str(match.get("adapter_id") or "")
    venue = re.sub(r"[^a-z0-9]+", "_", identity["venue"].lower()).strip("_")
    required = ",".join(sorted(identity["required_capabilities"]))
    return f"{adapter or venue}|{required}"


def reconcile_adapter_specs(conn: sqlite3.Connection) -> dict:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            select id, created_at, source_recommendation_id, market_key, priority,
                   title, status, spec_json, evidence_json
            from adapter_specs
            where status in (
                'open', 'adapter_capability_gap', 'resolved_existing_adapter_capability',
                'deployed_waiting_acceptance', 'implementation_failed_review'
            )
            order by priority desc, id asc
            """
        ).fetchall()
    ]
    inventory = capability_inventory()
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for row in rows:
        match = match_adapter_spec(row, inventory)
        groups.setdefault(_canonical_key(row, match), []).append((row, match))
    counts = Counter()
    details = []
    for _key, items in groups.items():
        for index, (row, match) in enumerate(items):
            evidence = _json(row.get("evidence_json"))
            evidence["adapter_capability_reconciliation"] = {**match, "checked_at": _utc_now()}
            if index > 0:
                status = "superseded_duplicate_adapter_spec"
                evidence["canonical_spec_id"] = items[0][0]["id"]
            elif match["match_status"] == "fully_covered":
                status = (
                    "implemented_runtime_adapter"
                    if row.get("status") == "deployed_waiting_acceptance"
                    else "resolved_existing_adapter_capability"
                )
            elif row.get("status") == "deployed_waiting_acceptance":
                status = "deployed_acceptance_failed"
            elif row.get("status") == "implementation_failed_review":
                status = "implementation_failed_review"
            elif match["match_status"] == "partial_capability_gap":
                status = "adapter_capability_gap"
            else:
                status = "open"
            conn.execute(
                "update adapter_specs set status = ?, evidence_json = ? where id = ?",
                (status, json.dumps(evidence, sort_keys=True), row["id"]),
            )
            counts[status] += 1
            details.append({"spec_id": row["id"], "title": row["title"], "status": status, **match})
    conn.commit()
    report = {
        "generated_at": _utc_now(),
        "summary": {
            "inventory_count": len(inventory),
            "specs_reconciled": len(details),
            "by_status": dict(counts),
        },
        "inventory": inventory,
        "specs": details,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Adapter Capability Inventory",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Runtime adapters: `{len(inventory)}`",
        f"Specs reconciled: `{len(details)}`",
        f"Statuses: `{dict(counts)}`",
        "",
        "## Capability Gaps",
        "",
    ]
    gaps = [item for item in details if item["status"] == "adapter_capability_gap"]
    if not gaps:
        lines.append("No open partial adapter capability gaps.")
    for item in gaps[:40]:
        lines.append(
            f"- Spec `#{item['spec_id']}` -> `{item.get('adapter_id')}` missing "
            f"`{item.get('missing_capabilities')}`"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
