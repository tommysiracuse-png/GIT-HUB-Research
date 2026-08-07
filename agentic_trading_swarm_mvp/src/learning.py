"""Outcome-driven learning for signal families."""

from __future__ import annotations

import pathlib
import json
import sqlite3

from storage import (
    RUNS_DIR,
    add_growth_experiment,
    add_improvement_task,
    bounded_paper_trade_lineage_valid,
    open_experiments,
    open_tasks,
    reliable_paper_label_eligibility_for_trade_row,
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def is_conditional_frontier_short_signal(signal_key: object) -> bool:
    """Return whether a signal key identifies a conditional frontier short cell.

    Synthetic research keys retain their direct signal key after the prefix, so
    token matching keeps their learning calibration isolated from unrelated
    synthetic experiments.
    """

    tokens = {token for token in str(signal_key or "").lower().split("|") if token}
    return (
        "frontier_crypto_venue_map" in tokens
        and "conditional" in tokens
        and any("short_frontier" in token for token in tokens)
    )


def adjustment_sample_requirement(signal_key: object, learning_cfg: dict) -> int:
    """Return the evidence requirement before a learned score can be trusted."""

    generic_minimum = max(1, int(learning_cfg["min_samples_for_adjustment"]))
    if not is_conditional_frontier_short_signal(signal_key):
        return generic_minimum
    return max(
        generic_minimum,
        int(learning_cfg.get("conditional_frontier_short_min_samples_for_adjustment", 20)),
    )


def update_signal_stats(conn: sqlite3.Connection, settings: dict) -> dict[str, dict]:
    learning_cfg = settings["learning"]
    max_adj = float(learning_cfg["max_adjustment_bps"])
    scale = float(learning_cfg["score_adjustment_scale"])

    rows = conn.execute(
        """
        select id, signal_key, pnl_bps, candidate_json, review_json, context_json,
               close_measurement_status, admission_key, admission_episode_id
        from paper_trades
        where status = 'closed' and pnl_bps is not null
        """
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        if not bounded_paper_trade_lineage_valid(conn, row, settings):
            continue
        grouped.setdefault(row["signal_key"], []).append(float(row["pnl_bps"]))

    existing_keys = {
        str(row["signal_key"])
        for row in conn.execute("select signal_key from signal_stats").fetchall()
    }
    for stale_key in existing_keys.difference(grouped):
        conn.execute("delete from signal_stats where signal_key = ?", (stale_key,))

    stats = {}
    for key, pnls in grouped.items():
        closed_count = len(pnls)
        wins = sum(1 for pnl in pnls if pnl > 0)
        avg = sum(pnls) / closed_count if closed_count else 0.0
        win_rate = wins / closed_count if closed_count else 0.0
        adjustment_min_samples = adjustment_sample_requirement(key, learning_cfg)
        if closed_count >= adjustment_min_samples:
            adjustment = clamp((avg * scale) + ((win_rate - 0.5) * 12.0), -max_adj, max_adj)
        else:
            adjustment = 0.0
        conn.execute(
            """
            insert into signal_stats (
                signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
            ) values (?, ?, ?, ?, ?, ?, datetime('now'))
            on conflict(signal_key) do update set
                closed_count = excluded.closed_count,
                wins = excluded.wins,
                avg_pnl_bps = excluded.avg_pnl_bps,
                win_rate = excluded.win_rate,
                score_adjustment = excluded.score_adjustment,
                updated_at = excluded.updated_at
            """,
            (key, closed_count, wins, round(avg, 3), round(win_rate, 3), round(adjustment, 3)),
        )
        stats[key] = {
            "closed_count": closed_count,
            "wins": wins,
            "avg_pnl_bps": round(avg, 3),
            "win_rate": round(win_rate, 3),
            "score_adjustment": round(adjustment, 3),
            "score_adjustment_min_samples": adjustment_min_samples,
            "score_adjustment_sample_confidence": round(
                min(1.0, closed_count / adjustment_min_samples), 3
            ),
        }
    conn.commit()
    update_contextual_stats(conn, settings)
    if bool(learning_cfg.get("task_emission_enabled", True)):
        generate_improvement_tasks(conn, stats, settings)
    if bool(learning_cfg.get("growth_experiment_emission_enabled", True)):
        generate_growth_experiments(conn, stats, settings)
    write_backlog(conn)
    write_growth_plan(conn)
    return stats


def update_contextual_stats(
    conn: sqlite3.Connection,
    settings: dict | None = None,
) -> None:
    rows = conn.execute(
        """
        select id, signal_key, pnl_bps, candidate_json, review_json, context_json,
               close_measurement_status, admission_key, admission_episode_id
        from paper_trades
        where status = 'closed' and pnl_bps is not null and context_json is not null
        """
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        if not bounded_paper_trade_lineage_valid(conn, row, settings):
            continue
        try:
            context = json.loads(row["context_json"] or "{}")
        except json.JSONDecodeError:
            context = {}
        stats_scope = str(context.get("signal_stats_scope") or "direct")
        if stats_scope == "paper_proxy":
            context_prefix = "paper_proxy|"
        elif stats_scope == "synthetic_research":
            context_prefix = "synthetic_research|"
        else:
            context_prefix = ""
        keys = [
            f"{context_prefix}venue:{context.get('venue')}",
            f"{context_prefix}trade_type:{context.get('trade_type')}",
            f"{context_prefix}direction:{context.get('direction')}",
            f"{context_prefix}feasibility:{context.get('feasibility_status')}",
            f"{context_prefix}liquidity:{context.get('liquidity_bucket')}",
            f"{context_prefix}spread:{context.get('spread_bucket')}",
            f"{context_prefix}region:{context.get('region')}",
            f"{context_prefix}asset_class:{context.get('asset_class')}",
        ]
        for key in keys:
            grouped.setdefault(key, []).append(float(row["pnl_bps"]))

    existing_keys = {
        str(row["context_key"])
        for row in conn.execute("select context_key from contextual_stats").fetchall()
    }
    retained_keys = {key for key in grouped if key and not key.endswith(":None")}
    for stale_key in existing_keys.difference(retained_keys):
        conn.execute("delete from contextual_stats where context_key = ?", (stale_key,))

    for key, pnls in grouped.items():
        if not key or key.endswith(":None"):
            continue
        wins = sum(1 for pnl in pnls if pnl > 0)
        avg = sum(pnls) / len(pnls)
        win_rate = wins / len(pnls)
        conn.execute(
            """
            insert into contextual_stats (
                context_key, closed_count, wins, avg_pnl_bps, win_rate, updated_at
            ) values (?, ?, ?, ?, ?, datetime('now'))
            on conflict(context_key) do update set
                closed_count = excluded.closed_count,
                wins = excluded.wins,
                avg_pnl_bps = excluded.avg_pnl_bps,
                win_rate = excluded.win_rate,
                updated_at = excluded.updated_at
            """,
            (key, len(pnls), wins, round(avg, 3), round(win_rate, 3)),
        )
    conn.commit()


def load_adjustments(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("select signal_key, score_adjustment from signal_stats").fetchall()
    return {row["signal_key"]: float(row["score_adjustment"]) for row in rows}


def generate_improvement_tasks(conn: sqlite3.Connection, stats: dict[str, dict], settings: dict) -> None:
    for key, item in stats.items():
        if item["closed_count"] < settings["learning"]["min_samples_for_adjustment"]:
            continue
        if item["avg_pnl_bps"] < -5:
            add_improvement_task(
                conn,
                90,
                f"Tighten filters for losing signal family: {key}",
                f"{key} has avg_pnl_bps={item['avg_pnl_bps']} over {item['closed_count']} closed paper trades.",
            )
        if item["win_rate"] < 0.35:
            add_improvement_task(
                conn,
                80,
                f"Red-team weak win-rate signal family: {key}",
                f"{key} win_rate={item['win_rate']} suggests the agent review is missing a failure mode.",
            )
        if item["avg_pnl_bps"] > 5 and item["win_rate"] > 0.55:
            add_improvement_task(
                conn,
                70,
                f"Expand profitable signal family: {key}",
                f"{key} is showing positive paper expectancy; add more venues/data and run longer hold-time tests.",
            )

    conditional_count = conn.execute(
        """
        select count(*) as n
        from opportunities
        where review_json like '%"feasibility_status": "conditional"%'
        """
    ).fetchone()["n"]
    if conditional_count >= 10:
        add_improvement_task(
            conn,
            75,
            "Add borrow and margin capability adapter",
            "Many high-scoring ideas are conditional because spot borrow, margin, or permissions are unknown.",
        )


def generate_growth_experiments(conn: sqlite3.Connection, stats: dict[str, dict], settings: dict) -> None:
    min_samples = settings["learning"]["min_samples_for_adjustment"]
    for key, item in stats.items():
        if item["closed_count"] < min_samples:
            add_growth_experiment(
                conn,
                40,
                key,
                "Need more observations before trusting this signal family.",
                "Continue collecting paper trades without changing live behavior.",
                item,
            )
            continue

        if item["avg_pnl_bps"] > 3 and item["win_rate"] >= 0.5:
            add_growth_experiment(
                conn,
                85,
                key,
                "Positive expectancy may improve with broader venue coverage.",
                "Add another venue adapter or expand scan universe for this signal family.",
                item,
            )
        if item["avg_pnl_bps"] > 0 and item["win_rate"] < 0.45:
            add_growth_experiment(
                conn,
                80,
                key,
                "Positive average with weak win rate suggests rare winners and noisy entries.",
                "Test stricter entry filters: higher net edge, lower spread, shorter time stop.",
                item,
            )
        if item["avg_pnl_bps"] < 0:
            add_growth_experiment(
                conn,
                90,
                key,
                "Negative expectancy signal family should be demoted or blocked.",
                "Increase score penalty and require additional confirmation before paper entry.",
                item,
            )

    conditional_count = conn.execute(
        """
        select count(*) as n
        from opportunities
        where review_json like '%"feasibility_status": "conditional"%'
        """
    ).fetchone()["n"]
    if conditional_count >= 10:
        add_growth_experiment(
            conn,
            75,
            "conditional_opportunities",
            "High-scoring blocked trades may become usable if borrow/margin capability is measured.",
            "Build an account capability adapter for spot borrow, margin, venue permissions, fees, and balances.",
            {"conditional_opportunity_count": conditional_count},
        )


def write_backlog(conn: sqlite3.Connection) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "improvement_backlog.md"
    tasks = open_tasks(conn)
    lines = ["# Improvement Backlog", ""]
    if not tasks:
        lines.append("No open tasks yet.")
    for task in tasks:
        lines.append(f"- P{task['priority']} #{task['id']}: {task['title']}")
        lines.append(f"  - {task['rationale']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_growth_plan(conn: sqlite3.Connection) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "growth_plan.md"
    experiments = open_experiments(conn)
    tasks = open_tasks(conn)
    stats = stats_snapshot(conn)
    lines = [
        "# Automatic Growth Plan",
        "",
        "This file is generated from paper-trade outcomes. The system may adjust scores automatically, but it does not rewrite code, install dependencies, or place live trades.",
        "",
        "## Open Experiments",
        "",
    ]
    if not experiments:
        lines.append("No open experiments yet.")
    for experiment in experiments[:20]:
        lines.append(f"- P{experiment['priority']} #{experiment['id']}: {experiment['hypothesis']}")
        lines.append(f"  - Signal: `{experiment['signal_key']}`")
        lines.append(f"  - Action: {experiment['action']}")
        lines.append(f"  - Evidence: {experiment['evidence']}")
    lines.extend(["", "## Open Build Tasks", ""])
    if not tasks:
        lines.append("No open build tasks yet.")
    for task in tasks[:20]:
        lines.append(f"- P{task['priority']} #{task['id']}: {task['title']}")
        lines.append(f"  - {task['rationale']}")
    research_path = RUNS_DIR / "research_worker_latest.json"
    if research_path.exists():
        try:
            research = json.loads(research_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            research = {}
        summary = research.get("summary", {})
        lines.extend(["", "## Global Market Discovery", ""])
        lines.append(
            f"- Candidates this run: `{summary.get('candidate_count', 0)}`, "
            f"new `{summary.get('new_candidate_count', 0)}`, "
            f"total known `{summary.get('total_known_candidate_count', 0)}`"
        )
        lines.append(f"- Surface types: `{summary.get('by_surface_type', {})}`")
        lines.append(f"- Regions: `{summary.get('by_region', {})}`")
        lines.append(f"- Artifact inserts: `{summary.get('inserted_artifact_counts', {})}`")
        for item in summary.get("top_candidates", [])[:10]:
            lines.append(
                f"- P{item.get('priority')} `{item.get('venue_or_source')}` "
                f"`{item.get('surface_type_classified')}` -> `{item.get('recommended_next_action')}`"
            )
    lines.extend(["", "## Signal Stats", ""])
    if not stats:
        lines.append("No closed signal stats yet.")
    for item in stats[:20]:
        lines.append(
            f"- `{item['signal_key']}`: n={item['closed_count']}, "
            f"avg={item['avg_pnl_bps']}bps, win_rate={item['win_rate']}, "
            f"score_adjustment={item['score_adjustment']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def stats_snapshot(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        order by closed_count desc, signal_key asc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def contextual_stats_snapshot(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select context_key, closed_count, wins, avg_pnl_bps, win_rate, updated_at
        from contextual_stats
        order by closed_count desc, abs(avg_pnl_bps) desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
