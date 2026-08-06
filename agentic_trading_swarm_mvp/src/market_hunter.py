"""Moving-target market hunter.

This module turns paper-trade evidence into market allocation and expansion
directives. It is the part of the swarm that asks: what should we study next,
what is decaying, and what deserves more connector work?
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3

from storage import (
    RUNS_DIR,
    active_signal_policies,
    add_growth_experiment,
    add_hunter_directive,
    add_improvement_task,
    open_hunter_directives,
    paper_label_eligibility_for_trade_row,
)


HYGIENE_JSON = RUNS_DIR / "hunter_directive_hygiene_report.json"
HYGIENE_MD = RUNS_DIR / "hunter_directive_hygiene_report.md"

IMPLEMENTED_CATEGORY_STATUSES = {
    "route_requirements": (
        "implemented_route_requirements",
        ("improvement_tasks", "route_probe_tasks"),
    ),
    "frontier_data_quality": (
        "implemented_frontier_data_quality",
        ("improvement_tasks", "adapter_specs"),
    ),
    "failure_diagnostics": (
        "implemented_failure_diagnostics",
        ("improvement_tasks", "adapter_specs"),
    ),
    "signal_redesign": (
        "implemented_signal_redesign",
        ("improvement_tasks", "adapter_specs"),
    ),
    "strategy_reliability_pack": (
        "implemented_strategy_reliability_pack",
        ("improvement_tasks", "growth_experiments"),
    ),
    "regional_fx_frontier_prediction_pack": (
        "implemented_regional_fx_frontier_prediction_pack",
        ("route_probe_tasks", "improvement_tasks", "adapter_specs"),
    ),
    "okx_basis_signal_research": (
        "implemented_okx_basis_signal_research",
        ("adapter_specs",),
    ),
    "okx_reliable_outcomes": (
        "implemented_okx_reliable_outcomes",
        ("improvement_tasks",),
    ),
    "self_improvement_open_pack": (
        "implemented_self_improvement_open_pack_2026_06_29",
        ("improvement_tasks", "growth_experiments"),
    ),
    "global_market_discovery_scan": (
        "implemented_global_market_discovery_scan",
        ("adapter_specs", "growth_experiments", "route_probe_tasks", "market_hunter_directives"),
    ),
}


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        select signal_key, opened_at, closed_at, pnl_bps, candidate_json, review_json, context_json
        from paper_trades
        where status = 'closed' and pnl_bps is not null
        order by closed_at asc, id asc
        """
    ).fetchall()
    return [
        row
        for row in rows
        if paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]
    ]


def _market_from_signal(signal_key: str) -> str:
    parts = signal_key.split("|")
    if len(parts) >= 2:
        return f"{parts[0]}|{parts[1]}"
    return signal_key


def _active_safety_governors(conn: sqlite3.Connection) -> dict[str, dict]:
    governors = {}
    for policy in active_signal_policies(conn):
        if policy.get("policy_type") == "safety_governor":
            governors[policy["signal_key"]] = policy
    return governors


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _classify_implemented_directive(item: dict) -> str | None:
    primary_text = " ".join(
        str(item.get(key, ""))
        for key in ("market_key", "directive", "rationale")
    ).lower()
    evidence = item.get("evidence") or {}
    text = primary_text + " " + json.dumps(evidence, sort_keys=True).lower()
    if "global_market_discovery" in text or "global discovery" in text or primary_text.startswith("global_discovery|"):
        return "global_market_discovery_scan"
    if "okx" in primary_text and "reliable" in primary_text and any(
        term in primary_text for term in ("label", "outcome", "legacy_unverified")
    ):
        return "okx_reliable_outcomes"
    if "okx" in primary_text and any(term in primary_text for term in ("basis", "funding", "perp")):
        return "okx_basis_signal_research"
    if "route" in text and any(term in text for term in ("requirement", "resolver", "conditional", "borrow", "permission")):
        return "route_requirements"
    if "frontier" in text and any(term in text for term in ("quality", "depth", "order book", "slippage", "freshness")):
        return "frontier_data_quality"
    if any(term in text for term in ("failure filter", "demote_or_filter", "decay_watch", "weak win", "weak win-rate")):
        return "strategy_reliability_pack"
    if any(term in text for term in ("signal redesign", "root cause", "root-cause", "poorly performing")):
        return "signal_redesign"
    if any(term in text for term in ("regional fx", "africa rail", "prediction", "expired", "adaptive depth")):
        return "regional_fx_frontier_prediction_pack"
    if any(term in text for term in ("spot borrow", "kalshi", "africa public rail", "yellow card", "bitnob")):
        return "self_improvement_open_pack"
    return None


def _implemented_category_exists(conn: sqlite3.Connection, category: str | None) -> bool:
    if not category or category not in IMPLEMENTED_CATEGORY_STATUSES:
        return False
    status, tables = IMPLEMENTED_CATEGORY_STATUSES[category]
    for table in tables:
        row = conn.execute(f"select 1 from {table} where status = ? limit 1", (status,)).fetchone()
        if row:
            return True
    return False


def clean_stale_hunter_directives(conn: sqlite3.Connection, settings: dict | None = None) -> dict:
    cfg = (settings or {}).get("hunter_directive_hygiene", {})
    if not cfg.get("enabled", True):
        return {"enabled": False, "closed_count": 0}
    restored = conn.execute(
        """
        update market_hunter_directives
        set status = 'open'
        where status = 'superseded_by_implemented_okx_basis_signal_research'
          and market_key not like '%OKX%'
        """
    ).rowcount
    conn.commit()
    before = open_hunter_directives(conn)
    closed: list[dict] = []
    retained: list[dict] = []
    for item in before:
        category = _classify_implemented_directive(item)
        if category and _implemented_category_exists(conn, category):
            status = f"superseded_by_implemented_{category}"
            conn.execute(
                "update market_hunter_directives set status = ? where id = ? and status = 'open'",
                (status, item["id"]),
            )
            closed.append(
                {
                    "id": item["id"],
                    "market_key": item["market_key"],
                    "directive": item["directive"],
                    "priority": item["priority"],
                    "implemented_category": category,
                    "new_status": status,
                }
            )
        else:
            retained.append(item)
    conn.commit()
    after = open_hunter_directives(conn)
    by_category: dict[str, int] = {}
    for item in closed:
        category = item["implemented_category"]
        by_category[category] = by_category.get(category, 0) + 1
    report = {
        "enabled": True,
        "generated_at": _utc_now(),
        "before_open_count": len(before),
        "after_open_count": len(after),
        "closed_count": len(closed),
        "restored_misclassified_count": int(restored or 0),
        "closed_by_category": by_category,
        "closed": closed[:200],
        "retained_open": after[:50],
        "hard_limits": [
            "Rows are status-updated, not deleted.",
            "Only directives matching already implemented manual categories are closed.",
            "Fresh unmatched directives remain open for the hunter swarm.",
        ],
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    HYGIENE_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    HYGIENE_MD.write_text(_hygiene_markdown(report), encoding="utf-8")
    return report


def _hygiene_markdown(report: dict) -> str:
    lines = [
        "# Hunter Directive Hygiene Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Before open: `{report.get('before_open_count')}`",
        f"- After open: `{report.get('after_open_count')}`",
        f"- Closed stale duplicates: `{report.get('closed_count')}`",
        f"- Restored prior misclassifications: `{report.get('restored_misclassified_count', 0)}`",
        f"- Closed by category: `{report.get('closed_by_category', {})}`",
        "",
        "## Closed Examples",
        "",
    ]
    for item in report.get("closed", [])[:30]:
        lines.append(
            f"- #{item.get('id')} `{item.get('market_key')}` `{item.get('directive')}` "
            f"-> `{item.get('new_status')}`"
        )
    lines.extend(["", "## Retained Open Directives", ""])
    retained = report.get("retained_open", [])
    if not retained:
        lines.append("No open directives remain after stale duplicate cleanup.")
    for item in retained[:30]:
        lines.append(f"- #{item.get('id')} P{item.get('priority')} `{item.get('directive')}` `{item.get('market_key')}`")
    return "\n".join(lines) + "\n"



def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "avg": 0.0, "win_rate": 0.0, "best": None, "worst": None}
    wins = sum(1 for value in values if value > 0)
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 3),
        "win_rate": round(wins / len(values), 3),
        "best": round(max(values), 3),
        "worst": round(min(values), 3),
    }


def analyze_markets(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    cfg = settings.get("hunter", {})
    min_samples = int(cfg.get("min_samples_to_classify", 5))
    promote_avg = float(cfg.get("promotion_avg_bps", 5.0))
    promote_wr = float(cfg.get("promotion_win_rate", 0.55))
    demote_avg = float(cfg.get("demotion_avg_bps", -5.0))
    decay_threshold = float(cfg.get("decay_recent_vs_lifetime_bps", -10.0))

    by_signal: dict[str, list[float]] = {}
    by_market: dict[str, list[float]] = {}
    safety_governors = _active_safety_governors(conn)
    for row in _rows(conn):
        pnl = float(row["pnl_bps"])
        signal = row["signal_key"]
        market = _market_from_signal(signal)
        by_signal.setdefault(signal, []).append(pnl)
        by_market.setdefault(market, []).append(pnl)

    directives = []

    for key, values in sorted(by_signal.items()):
        lifetime = _stats(values)
        recent_values = values[-min(10, len(values)) :]
        recent = _stats(recent_values)
        evidence = {"lifetime": lifetime, "recent": recent}
        directive = "observe"
        priority = 35
        rationale = "Not enough evidence yet; preserve exploration budget."

        if lifetime["count"] >= min_samples:
            recent_delta = recent["avg"] - lifetime["avg"]
            evidence["recent_delta_bps"] = round(recent_delta, 3)
            if lifetime["avg"] >= promote_avg and lifetime["win_rate"] >= promote_wr:
                directive = "exploit_more"
                priority = 85
                rationale = "Signal family has positive paper expectancy and acceptable win rate."
            elif lifetime["avg"] <= demote_avg:
                directive = "demote_or_filter"
                priority = 90
                rationale = "Signal family is losing after paper-trade outcomes."
            elif recent_delta <= decay_threshold:
                directive = "decay_watch"
                priority = 80
                rationale = "Recent performance is meaningfully worse than lifetime performance."
            elif lifetime["win_rate"] < 0.4:
                directive = "red_team"
                priority = 75
                rationale = "Weak win rate suggests missing failure filters."

        governor = safety_governors.get(key)
        if governor and directive in {"demote_or_filter", "decay_watch", "red_team"}:
            policy = governor.get("policy", {})
            evidence["safety_governor"] = {
                "policy_id": governor["policy_id"],
                "mode": policy.get("governor_mode"),
                "release_criteria": policy.get("release_criteria"),
                "applied_count": governor.get("applied_count"),
                "filtered_count": governor.get("filtered_count"),
                "opened_count": governor.get("opened_count"),
            }
            directive = "safety_governor_active"
            priority = 45
            rationale = "Persistent safety governor already handles this signal; monitor recovery probes and release criteria."

        directives.append(
            {
                "market_key": key,
                "directive": directive,
                "priority": priority,
                "rationale": rationale,
                "evidence": evidence,
            }
        )

    for key, values in sorted(by_market.items()):
        market_stats = _stats(values)
        if market_stats["count"] >= min_samples and market_stats["avg"] <= demote_avg:
            directives.append(
                {
                    "market_key": key,
                    "directive": "market_decay_watch",
                    "priority": 70,
                    "rationale": "Whole market adapter is currently losing; preserve exploration but do not over-allocate.",
                    "evidence": {"market": market_stats},
                }
            )

    uncovered = _uncovered_market_directives(conn)
    directives.extend(uncovered)
    directives.sort(key=lambda item: item["priority"], reverse=True)
    return directives


def _uncovered_market_directives(conn: sqlite3.Connection) -> list[dict]:
    directives = []
    conditional_count = conn.execute(
        """
        select count(*) as n
        from opportunities
        where decision = 'approve_conditional_paper_trade' or decision = 'conditional_review'
        """
    ).fetchone()["n"]
    if conditional_count >= 25:
        route_report = RUNS_DIR / "route_resolver_report.json"
        if route_report.exists():
            try:
                report = json.loads(route_report.read_text(encoding="utf-8"))
                route_summary = report.get("summary", {})
            except json.JSONDecodeError:
                route_summary = {}
            directives.append(
                {
                    "market_key": "execution_routes",
                    "directive": "route_resolver_active",
                    "priority": 50,
                    "rationale": "Route resolver is active; monitor missing route requirements instead of reopening the build task.",
                    "evidence": {
                        "conditional_count": int(conditional_count),
                        "by_missing_requirement": route_summary.get("by_missing_requirement", {}),
                        "by_route_status": route_summary.get("by_route_status", {}),
                    },
                }
            )
        else:
            directives.append(
                {
                    "market_key": "execution_routes",
                    "directive": "expand_route_resolver",
                    "priority": 88,
                    "rationale": "Conditional opportunities are accumulating; the hunter needs broker/borrow/permission discovery.",
                    "evidence": {"conditional_count": int(conditional_count)},
                }
            )

    global_proxy_count = conn.execute(
        """
        select count(*) as n
        from opportunities
        where trade_type = 'global_proxy_momentum'
        """
    ).fetchone()["n"]
    if global_proxy_count < 20:
        directives.append(
            {
                "market_key": "global_proxy_momentum",
                "directive": "collect_market_hours_data",
                "priority": 60,
                "rationale": "Global proxy scanner needs more market-hours observations before classification.",
                "evidence": {"global_proxy_observations": int(global_proxy_count)},
            }
        )
    return directives


def persist_hunter_directives(conn: sqlite3.Connection, directives: list[dict]) -> None:
    for item in directives:
        add_hunter_directive(
            conn,
            item["market_key"],
            item["directive"],
            item["priority"],
            item["rationale"],
            item["evidence"],
        )
        if item["directive"] in {"demote_or_filter", "decay_watch", "red_team"}:
            add_growth_experiment(
                conn,
                item["priority"],
                item["market_key"],
                item["rationale"],
                f"Hunter directive: {item['directive']}",
                item["evidence"],
            )
        if item["directive"] == "expand_route_resolver":
            add_improvement_task(
                conn,
                item["priority"],
                "Expand execution-route resolver for conditional markets",
                item["rationale"],
            )


def write_market_hunter_plan(conn: sqlite3.Connection, directives: list[dict]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "market_hunter_plan.md"
    open_items = open_hunter_directives(conn)
    lines = [
        "# Market Hunter Plan",
        "",
        "This file is generated automatically. It treats markets and signal families as moving targets, preserving exploration while demoting decaying edges.",
        "",
        f"- Directive hygiene report: `{HYGIENE_MD}`",
        "",
        "## Current Directives",
        "",
    ]
    items = directives or open_items
    if not items:
        lines.append("No directives yet.")
    for item in items[:30]:
        lines.append(f"- P{item['priority']} `{item['directive']}` for `{item['market_key']}`")
        lines.append(f"  - {item['rationale']}")
        lines.append(f"  - Evidence: {item['evidence']}")

    lines.extend(
        [
            "",
            "## Hunter Swarm Roles",
            "",
            "- Market Scout: finds markets/adapters with too little coverage.",
            "- Edge Rotator: increases/decreases paper allocation by recent evidence.",
            "- Decay Watcher: flags signal families whose edge is fading.",
            "- Route Hunter: turns conditional paper edges into broker/execution-route tasks.",
            "- Red-Team Hunter: explains why profitable-looking markets are failing.",
            "",
            "## Operating Rule",
            "",
            "Do not expect one market to stay profitable. Keep exploration alive, rotate allocation, and promote only edges that keep working out-of-sample.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_market_hunter(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    if not settings.get("hunter", {}).get("enabled", True):
        return []
    directives = analyze_markets(conn, settings)
    persist_hunter_directives(conn, directives)
    clean_stale_hunter_directives(conn, settings)
    open_items = open_hunter_directives(conn)
    write_market_hunter_plan(conn, open_items)
    return open_items
