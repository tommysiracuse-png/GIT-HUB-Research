"""Replay stored candidates through the current paper-admission code."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

from agent_review import review_candidate
from paper_exploration import prepare_candidate_for_exploration
from settings import load_settings


APPROVED = {"approve_paper_trade", "approve_conditional_paper_trade"}


def _json(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lineage(candidate: dict) -> str:
    return "|".join(
        (
            str(candidate.get("strategy_lab_id") or candidate.get("signal_lineage_key") or candidate.get("trade_type") or "unknown"),
            str(candidate.get("venue") or "unknown"),
            str(candidate.get("direction") or "unknown"),
            str(candidate.get("signal_variant_id") or candidate.get("strategy_lab_version") or "base"),
        )
    )


def replay_candidates(candidates: list[dict], settings: dict) -> dict:
    seen: Counter[str] = Counter()
    admitted: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    synthetic: Counter[str] = Counter()
    route_contradictions: Counter[str] = Counter()
    for raw in candidates:
        lineage = _lineage(raw)
        seen[lineage] += 1
        try:
            candidate = prepare_candidate_for_exploration(dict(raw), settings)
            review = review_candidate(candidate, settings, {}, policies=[])
        except Exception as exc:  # noqa: BLE001 - replay must report malformed historical candidates
            errors[f"{type(exc).__name__}: {str(exc)[:120]}"] += 1
            continue
        if review.get("decision") in APPROVED:
            admitted[lineage] += 1
            if candidate.get("synthetic_research_paper"):
                synthetic[lineage] += 1
        registry = raw.get("paper_route_registry") or {}
        if registry.get("action") in {"allow", "paper_fill"} and candidate.get("synthetic_research_paper"):
            route_contradictions[lineage] += 1
    return {
        "candidate_count": len(candidates),
        "seen_by_lineage": dict(seen),
        "admitted_by_lineage": dict(admitted),
        "synthetic_by_lineage": dict(synthetic),
        "route_contradictions": dict(route_contradictions),
        "errors": dict(errors),
    }


def replay_database(db_path: str, limit: int = 1000) -> dict:
    uri = f"file:{db_path.replace('\\', '/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select candidate_json
            from opportunities
            where candidate_json is not null
            order by id desc
            limit ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return replay_candidates([_json(row["candidate_json"]) for row in rows], load_settings())


def compare_replays(before: dict, after: dict, allowed_zero: set[str] | None = None) -> dict:
    allowed_zero = allowed_zero or set()
    collapsed = []
    before_admitted = before.get("admitted_by_lineage") or {}
    after_admitted = after.get("admitted_by_lineage") or {}
    for lineage, count in before_admitted.items():
        if int(count or 0) > 0 and int(after_admitted.get(lineage) or 0) == 0 and lineage not in allowed_zero:
            collapsed.append(lineage)
    return {
        "passed": not collapsed and not after.get("errors") and not after.get("route_contradictions"),
        "status": (
            "passed"
            if not collapsed and not after.get("errors") and not after.get("route_contradictions")
            else "paper_admission_replay_failed"
        ),
        "collapsed_lineages": collapsed,
        "before": before,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    result = replay_database(args.db, args.limit)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
