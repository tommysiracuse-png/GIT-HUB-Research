"""Bulk loading for paired signal-variant horizon outcomes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


def load_variant_horizon_outcomes(
    conn: sqlite3.Connection,
    variant_ids: list[str],
    horizons: list[int],
    metrics_fn: Callable[[list[float]], dict],
    percentile_fn: Callable[[list[float], float], float],
) -> dict[tuple[str, int], dict]:
    variants = list(dict.fromkeys(str(value) for value in variant_ids if value))
    horizon_values = sorted({int(value) for value in horizons if int(value) > 0})
    if not variants or not horizon_values:
        return {}

    variant_marks = ",".join("?" for _ in variants)
    horizon_marks = ",".join("?" for _ in horizon_values)
    trial_rows = conn.execute(
        f"""
        select id, variant_id, pair_key, created_at
        from signal_trials
        where eligible = 1 and variant_id in ({variant_marks})
        order by created_at asc
        """,
        variants,
    ).fetchall()
    trials_by_variant: dict[str, list[dict]] = {variant_id: [] for variant_id in variants}
    trial_lookup = {}
    for row in trial_rows:
        item = dict(row)
        trial_id = int(item["id"])
        trial_lookup[trial_id] = item
        trials_by_variant.setdefault(str(item["variant_id"]), []).append(item)

    outcomes_by_key = {}
    if trial_lookup:
        outcome_rows = conn.execute(
            f"""
            select o.trial_id, o.horizon_minutes, o.pnl_bps, o.delay_seconds,
                   o.measurement_status
            from signal_trial_outcomes o
            join signal_trials t on t.id = o.trial_id
            where t.eligible = 1
              and t.variant_id in ({variant_marks})
              and o.horizon_minutes in ({horizon_marks})
            """,
            [*variants, *horizon_values],
        ).fetchall()
        outcomes_by_key = {
            (int(row["trial_id"]), int(row["horizon_minutes"])): dict(row)
            for row in outcome_rows
        }

    cache = {}
    for variant_id in variants:
        trials = trials_by_variant.get(variant_id, [])
        for horizon in horizon_values:
            valid = {}
            due_count = 0
            delays = []
            for trial in trials:
                outcome = outcomes_by_key.get((int(trial["id"]), horizon))
                if outcome is None:
                    continue
                due_count += 1
                if outcome.get("measurement_status") != "valid" or outcome.get("pnl_bps") is None:
                    continue
                valid[str(trial["pair_key"])] = float(outcome["pnl_bps"])
                if outcome.get("delay_seconds") is not None:
                    delays.append(float(outcome["delay_seconds"]))
            cache[(variant_id, horizon)] = {
                "rows": [],
                "valid": valid,
                "metrics": metrics_fn(list(valid.values())),
                "total_trials": len(trials),
                "due_trials": due_count,
                "valid_label_rate": round(len(valid) / due_count, 3) if due_count else None,
                "delay_p95_seconds": round(float(percentile_fn(delays, 0.95)), 3) if delays else None,
                "first_trial_at": trials[0]["created_at"] if trials else None,
                "last_trial_at": trials[-1]["created_at"] if trials else None,
            }
    return cache
