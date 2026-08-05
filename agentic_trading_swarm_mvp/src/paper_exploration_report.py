"""Reporting for exploration admissions and counterfactual paper guards."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter, defaultdict

from storage import RUNS_DIR


_GUARD_REASON_PATTERNS = (
    (re.compile(r"^learned score below threshold", re.I), "learned score below threshold"),
    (re.compile(r"^estimated net edge too small after costs", re.I), "estimated net edge too small after costs"),
    (re.compile(r"^liquidity score below minimum", re.I), "liquidity score below minimum"),
    (re.compile(r"^self-improvement min learned score", re.I), "self-improvement minimum learned score not met"),
    (re.compile(r"^self-improvement min net edge", re.I), "self-improvement minimum net edge not met"),
    (re.compile(r"^self-improvement max spread", re.I), "self-improvement maximum spread exceeded"),
    (re.compile(r"^paper context cost floor not cleared", re.I), "paper context cost floor not cleared"),
    (re.compile(r"^market data dangerously stale", re.I), "market data dangerously stale"),
)


def _reason_category(reason: object) -> str:
    text = str(reason or "unknown").strip()
    for pattern, category in _GUARD_REASON_PATTERNS:
        if pattern.search(text):
            return category
    return text


def _load_json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _lineage(candidate: dict) -> str:
    return "|".join(
        (
            str(candidate.get("strategy_lab_id") or candidate.get("signal_lineage_key") or candidate.get("trade_type") or "unknown"),
            str(candidate.get("venue") or "unknown"),
            str(candidate.get("direction") or "unknown"),
            str(candidate.get("signal_variant_id") or candidate.get("strategy_lab_version") or "base"),
        )
    )


def build_paper_exploration_report(
    conn: sqlite3.Connection,
    settings: dict,
    reviewed: list[dict] | None = None,
) -> dict:
    cfg = settings.get("paper_exploration", {})
    horizon = int(cfg.get("guard_value_horizon_minutes", 60))
    rows = conn.execute(
        """
        select p.id, p.status, p.pnl_bps, p.candidate_json, p.review_json,
               o.pnl_bps as horizon_pnl_bps, o.measurement_status
        from paper_trades p
        left join paper_trade_outcomes o
          on o.trade_id = p.id and o.horizon_minutes = ?
        """,
        (horizon,),
    ).fetchall()
    trade_scopes: Counter[str] = Counter()
    guard_trade_counts: Counter[str] = Counter()
    guard_pnls: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        candidate = _load_json(row["candidate_json"])
        review = _load_json(row["review_json"])
        scope = str(candidate.get("signal_stats_scope") or review.get("signal_stats_scope") or "direct")
        trade_scopes[scope] += 1
        reasons = review.get("would_block_reasons") or candidate.get("paper_exploration_would_block_reasons") or []
        for reason in dict.fromkeys(_reason_category(item) for item in reasons if item):
            guard_trade_counts[reason] += 1
            if row["measurement_status"] == "valid" and row["horizon_pnl_bps"] is not None:
                guard_pnls[reason].append(float(row["horizon_pnl_bps"]))

    guard_value = []
    for reason, count in guard_trade_counts.most_common():
        pnls = guard_pnls.get(reason, [])
        avoided = sum(-value for value in pnls if value < 0)
        missed = sum(value for value in pnls if value > 0)
        guard_value.append(
            {
                "guard_reason": reason,
                "would_block_trade_count": int(count),
                "valid_outcome_count": len(pnls),
                "avg_pnl_bps": round(sum(pnls) / len(pnls), 3) if pnls else None,
                "losses_would_have_avoided_bps": round(avoided, 3),
                "profits_would_have_missed_bps": round(missed, 3),
                "net_guard_value_bps": round(avoided - missed, 3),
            }
        )

    opportunity_rows = conn.execute(
        """
        select decision, candidate_json, review_json
        from opportunities
        where seen_at >= datetime('now', '-24 hours')
          and candidate_json like '%\"paper_exploration_enabled\": true%'
        """
    ).fetchall()
    decisions: Counter[str] = Counter(str(row["decision"] or "unknown") for row in opportunity_rows)
    true_rejections: Counter[str] = Counter()
    for row in opportunity_rows:
        review = _load_json(row["review_json"])
        if str(row["decision"] or "") not in {"reject", "reject_invalid_data"}:
            continue
        for reason in review.get("hard_blocks") or []:
            true_rejections[_reason_category(reason)] += 1

    prior_streaks: dict[str, int] = {}
    prior_path = RUNS_DIR / "paper_exploration_report.json"
    if prior_path.exists():
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            prior_streaks = {
                str(key): int(value)
                for key, value in (prior.get("admission_zero_streaks") or {}).items()
            }
        except (OSError, ValueError, TypeError):
            prior_streaks = {}
    historically_active = set()
    for row in conn.execute(
        "select candidate_json from paper_trades where candidate_json like '%\"paper_exploration_enabled\": true%'"
    ).fetchall():
        historically_active.add(_lineage(_load_json(row["candidate_json"])))
    current_priceable: Counter[str] = Counter()
    current_admitted: Counter[str] = Counter()
    admitted_decisions = {
        "approve_paper_trade",
        "approve_conditional_paper_trade",
        "deferred_capacity",
        "reject_duplicate_open_exposure",
    }
    for item in reviewed or []:
        candidate = item.get("candidate") or {}
        review = item.get("review") or {}
        lineage = _lineage(candidate)
        if candidate.get("paper_exploration_immutable_rejections"):
            continue
        current_priceable[lineage] += 1
        if review.get("decision") in admitted_decisions:
            current_admitted[lineage] += 1
    zero_streaks = {}
    for lineage in historically_active:
        if current_priceable.get(lineage, 0) <= 0:
            continue
        if current_admitted.get(lineage, 0) > 0:
            zero_streaks[lineage] = 0
        else:
            zero_streaks[lineage] = int(prior_streaks.get(lineage, 0)) + 1

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "enabled": bool(cfg.get("enabled", False)),
        "horizon_minutes": horizon,
        "summary": {
            "direct_paper_trades": int(trade_scopes.get("direct", 0)),
            "synthetic_paper_trades": int(trade_scopes.get("synthetic_research", 0)),
            "paper_proxy_trades": int(trade_scopes.get("paper_proxy", 0)),
            "true_invalid_data_rejections_24h": sum(true_rejections.values()),
            "capacity_deferrals_24h": int(decisions.get("deferred_capacity", 0)),
            "duplicate_exposure_rejections_24h": int(decisions.get("reject_duplicate_open_exposure", 0)),
            "would_block_guard_count": len(guard_value),
        },
        "decisions_24h": dict(decisions),
        "true_rejection_reasons_24h": dict(true_rejections.most_common()),
        "guard_value": guard_value,
        "current_cycle_lineages": {
            "priceable": dict(current_priceable),
            "admitted_or_deferred": dict(current_admitted),
        },
        "admission_zero_streaks": zero_streaks,
    }


def write_paper_exploration_report(
    conn: sqlite3.Connection,
    settings: dict,
    reviewed: list[dict] | None = None,
) -> dict:
    report = build_paper_exploration_report(conn, settings, reviewed=reviewed)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / "paper_exploration_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary = report["summary"]
    lines = [
        "# Paper Exploration Report",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Enabled: `{report['enabled']}`",
        "",
        "## Admission Summary",
        "",
        f"- Direct paper trades: `{summary['direct_paper_trades']}`",
        f"- Synthetic research trades: `{summary['synthetic_paper_trades']}`",
        f"- True invalid-data rejections (24h): `{summary['true_invalid_data_rejections_24h']}`",
        f"- Capacity deferrals (24h): `{summary['capacity_deferrals_24h']}`",
        "",
        "## Counterfactual Guard Value",
        "",
        "| Guard | Trades | Valid 60m | Avg bps | Losses avoided | Profits missed | Net value |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["guard_value"][:30]:
        lines.append(
            f"| {item['guard_reason']} | {item['would_block_trade_count']} | "
            f"{item['valid_outcome_count']} | {item['avg_pnl_bps']} | "
            f"{item['losses_would_have_avoided_bps']} | {item['profits_would_have_missed_bps']} | "
            f"{item['net_guard_value_bps']} |"
        )
    (RUNS_DIR / "paper_exploration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def compact_paper_exploration_report(report: dict, guard_limit: int = 10) -> dict:
    """Return the bounded view embedded in state packets and LLM context."""
    return {
        "generated_at": report.get("generated_at"),
        "enabled": report.get("enabled"),
        "horizon_minutes": report.get("horizon_minutes"),
        "summary": report.get("summary") or {},
        "decisions_24h": report.get("decisions_24h") or {},
        "true_rejection_reasons_24h": report.get("true_rejection_reasons_24h") or {},
        "top_guard_value": list(report.get("guard_value") or [])[: max(0, int(guard_limit))],
        "current_cycle_lineages": report.get("current_cycle_lineages") or {},
        "admission_zero_streaks": report.get("admission_zero_streaks") or {},
    }
