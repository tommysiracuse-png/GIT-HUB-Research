"""Persistent signal-family safety governor.

The self-improvement executor creates bounded policy experiments from LLM
recommendations. This module adds a durable, evidence-based guardrail on top:
bad signal families are quarantined or demoted until real paper recovery
evidence appears. Quarantine still allows tiny recovery probes so a changing
market can earn its way back instead of being blocked forever.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

from storage import RUNS_DIR, add_memory_fact, add_self_improvement_experiment, add_signal_policy, utc_now


REPORT_JSON = RUNS_DIR / "signal_safety_governor.json"
REPORT_MD = RUNS_DIR / "signal_safety_governor.md"


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _freshness_terms(cfg: dict) -> tuple[str, ...]:
    configured = cfg.get("stale_signal_terms")
    if isinstance(configured, (list, tuple, set)):
        terms = tuple(str(term).strip().lower() for term in configured if str(term).strip())
        if terms:
            return terms
    return ("opportunistic", "exploit")


def _freshness_sensitive_signal(signal_key: str, cfg: dict) -> bool:
    lowered = str(signal_key or "").lower()
    return any(term in lowered for term in _freshness_terms(cfg))


def _signal_age_seconds(updated_at: str | None) -> float | None:
    parsed = _parse_iso(updated_at)
    if not parsed:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def _freshness_guard(stats: dict, recent: dict, cfg: dict) -> tuple[str, str] | None:
    if not bool(cfg.get("stale_signal_guard_enabled", True)):
        return None
    if not _freshness_sensitive_signal(str(stats.get("signal_key") or ""), cfg):
        return None

    horizon_seconds = int(cfg.get("stale_signal_horizon_seconds", 6 * 3600) or 0)
    age_seconds = _signal_age_seconds(stats.get("updated_at"))
    if horizon_seconds > 0 and age_seconds is not None and age_seconds > horizon_seconds:
        return "quarantine", "stale_signal_decayed"

    min_recent_avg = float(cfg.get("stale_signal_min_recent_avg_bps", 0.0))
    recent_avg = recent.get("avg_pnl_bps")
    if recent_avg is not None and float(recent_avg) < min_recent_avg:
        return "quarantine", "stale_signal_decayed"

    return None


def _closed_metrics(conn: sqlite3.Connection, signal_key: str, *, since: str | None = None, limit: int | None = None) -> dict:
    params: list[object] = [signal_key]
    clause = "signal_key = ? and status = 'closed' and pnl_bps is not null"
    if since:
        clause += " and closed_at >= ?"
        params.append(since)
    sql = f"""
        select pnl_bps, closed_at
        from paper_trades
        where {clause}
        order by closed_at desc
    """
    if limit:
        sql += " limit ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows]
    if not pnls:
        return {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "best_bps": None, "worst_bps": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def _signal_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at
        from signal_stats
        order by avg_pnl_bps asc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _cfg(settings: dict) -> dict:
    defaults = {
        "enabled": True,
        "min_closed_for_action": 5,
        "recent_trade_window": 10,
        "quarantine_recent_avg_bps": -100.0,
        "quarantine_lifetime_avg_bps": -250.0,
        "quarantine_worst_bps": -1000.0,
        "quarantine_win_rate": 0.30,
        "demote_recent_avg_bps": -20.0,
        "demote_lifetime_avg_bps": -50.0,
        "demote_win_rate": 0.30,
        "recovery_probe_every_reviews": 25,
        "recovery_probe_allocation_multiplier": 0.10,
        "probation_allocation_multiplier": 0.25,
        "release_min_recovery_trades": 5,
        "release_min_avg_pnl_bps": 10.0,
        "release_min_win_rate": 0.55,
        "release_max_worst_bps": -500.0,
        "stale_signal_guard_enabled": True,
        "stale_signal_horizon_seconds": 6 * 3600,
        "stale_signal_min_recent_avg_bps": 0.0,
        "stale_signal_terms": ("opportunistic", "exploit"),
    }
    return {**defaults, **settings.get("signal_safety", {})}


def _classify_signal(stats: dict, recent: dict, cfg: dict) -> tuple[str, str]:
    closed = int(stats.get("closed_count") or 0)
    lifetime_avg = float(stats.get("avg_pnl_bps") or 0.0)
    lifetime_wr = float(stats.get("win_rate") or 0.0)
    recent_count = int(recent.get("closed_count") or 0)
    recent_avg = recent.get("avg_pnl_bps")
    recent_wr = recent.get("win_rate")
    worst = recent.get("worst_bps")

    freshness_override = _freshness_guard(stats, recent, cfg)
    if freshness_override:
        return freshness_override

    if closed < int(cfg["min_closed_for_action"]):
        return "observe", "not_enough_closed_trades"

    recent_bad = recent_count >= int(cfg["min_closed_for_action"]) and recent_avg is not None
    severe_recent = recent_bad and (
        float(recent_avg) <= float(cfg["quarantine_recent_avg_bps"])
        or (worst is not None and float(worst) <= float(cfg["quarantine_worst_bps"]))
        or (recent_wr is not None and float(recent_wr) <= float(cfg["quarantine_win_rate"]) and float(recent_avg) < 0)
    )
    severe_lifetime = lifetime_avg <= float(cfg["quarantine_lifetime_avg_bps"])
    if severe_recent or severe_lifetime:
        return "quarantine", "severe_negative_expectancy"

    demote_recent = recent_bad and (
        float(recent_avg) <= float(cfg["demote_recent_avg_bps"])
        or (recent_wr is not None and float(recent_wr) <= float(cfg["demote_win_rate"]) and float(recent_avg) < 0)
    )
    demote_lifetime = lifetime_avg <= float(cfg["demote_lifetime_avg_bps"]) or (
        lifetime_wr <= float(cfg["demote_win_rate"]) and lifetime_avg < 0
    )
    if demote_recent or demote_lifetime:
        return "probation", "negative_expectancy"

    return "healthy", "no_governor_action"


def _policy_id(signal_key: str) -> str:
    digest = hashlib.sha256(signal_key.encode("utf-8")).hexdigest()[:22]
    return f"sg_{digest}"


def _source_id(signal_key: str) -> str:
    return f"signal_safety_governor:{hashlib.sha256(signal_key.encode('utf-8')).hexdigest()[:24]}"


def _policy_for(mode: str, reason: str, stats: dict, recent: dict, settings: dict, cfg: dict) -> dict:
    risk = settings.get("risk", {})
    scanner = settings.get("scanner", {})
    severe = mode == "quarantine"
    return {
        "governor_mode": mode,
        "reason": reason,
        "min_score_delta": 18.0 if severe else 9.0,
        "min_net_edge_bps": max(float(risk.get("min_net_edge_bps", 2.0)) + (10.0 if severe else 4.0), 8.0 if severe else 5.0),
        "max_spread_bps": min(float(risk.get("max_spread_bps", 8.0)), 3.5 if severe else 5.0),
        "allocation_multiplier": float(cfg["recovery_probe_allocation_multiplier"] if severe else cfg["probation_allocation_multiplier"]),
        "pause_entries": severe,
        "expires_after_trades": None,
        "allow_recovery_probes": True,
        "recovery_probe_every_n_reviews": int(cfg["recovery_probe_every_reviews"]),
        "recovery_probe_allocation_multiplier": float(cfg["recovery_probe_allocation_multiplier"]),
        "release_criteria": {
            "min_closed_trades": int(cfg["release_min_recovery_trades"]),
            "min_avg_pnl_bps": float(cfg["release_min_avg_pnl_bps"]),
            "min_win_rate": float(cfg["release_min_win_rate"]),
            "max_worst_bps": float(cfg["release_max_worst_bps"]),
        },
        "base_min_score": float(scanner.get("min_base_score", 35.0)),
        "stats_at_activation": stats,
        "recent_at_activation": recent,
    }


def _existing_governor_policy(conn: sqlite3.Connection, signal_key: str) -> dict | None:
    row = conn.execute(
        """
        select policy_id, created_at, experiment_id, signal_key, policy_type, status,
               applied_count, filtered_count, opened_count, policy_json
        from signal_policies
        where signal_key = ? and policy_type = 'safety_governor'
        order by created_at desc
        limit 1
        """,
        (signal_key,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["policy"] = json.loads(item.pop("policy_json") or "{}")
    return item


def _recovery_metrics(conn: sqlite3.Connection, policy: dict, signal_key: str) -> dict:
    return _closed_metrics(conn, signal_key, since=policy.get("created_at"))


def _meets_release_criteria(metrics: dict, policy: dict) -> bool:
    criteria = policy.get("policy", {}).get("release_criteria", {})
    if int(metrics.get("closed_count") or 0) < int(criteria.get("min_closed_trades", 5)):
        return False
    if metrics.get("avg_pnl_bps") is None or float(metrics["avg_pnl_bps"]) < float(criteria.get("min_avg_pnl_bps", 10.0)):
        return False
    if metrics.get("win_rate") is None or float(metrics["win_rate"]) < float(criteria.get("min_win_rate", 0.55)):
        return False
    if metrics.get("worst_bps") is None or float(metrics["worst_bps"]) < float(criteria.get("max_worst_bps", -500.0)):
        return False
    return True


def _release_policy(conn: sqlite3.Connection, policy: dict, recovery: dict) -> dict:
    now = utc_now()
    conn.execute("update signal_policies set status = 'released' where policy_id = ?", (policy["policy_id"],))
    if policy.get("experiment_id"):
        conn.execute(
            """
            update self_improvement_experiments
            set status = 'released',
                decision = 'released_after_recovery_evidence',
                completed_at = ?,
                evaluation_json = ?,
                reflection = 'Signal family met recovery criteria after real paper outcomes; governor released it.'
            where id = ?
            """,
            (now, json.dumps({"recovery": recovery}, sort_keys=True), policy["experiment_id"]),
        )
    conn.commit()
    add_memory_fact(
        conn,
        "signal_safety",
        policy["signal_key"],
        "released",
        "recovery_evidence_met",
        0.88,
        "signal_safety_governor",
        {"policy_id": policy["policy_id"], "recovery": recovery},
    )
    return {"signal_key": policy["signal_key"], "policy_id": policy["policy_id"], "recovery": recovery}


def _upsert_policy(conn: sqlite3.Connection, signal_key: str, mode: str, reason: str, stats: dict, recent: dict, settings: dict, cfg: dict) -> dict:
    policy = _policy_for(mode, reason, stats, recent, settings, cfg)
    existing = _existing_governor_policy(conn, signal_key)
    pid = _policy_id(signal_key)
    source_id = _source_id(signal_key)
    baseline = _closed_metrics(conn, signal_key)
    action = (
        "Persistently quarantine signal while allowing recovery probes."
        if mode == "quarantine"
        else "Persistently demote signal until recovery evidence appears."
    )
    experiment_id = add_self_improvement_experiment(
        conn,
        source_id,
        "signal_safety_governor",
        "safety_governor",
        96 if mode == "quarantine" else 92,
        "|".join(signal_key.split("|")[:2]),
        signal_key,
        f"Signal safety governor should reduce exposure until {signal_key} recovers.",
        action,
        baseline,
        policy,
    )

    evidence = {"signal_stats": stats, "recent": recent, "baseline": baseline, "reason": reason}
    inserted = add_signal_policy(
        conn,
        pid,
        experiment_id,
        source_id,
        signal_key,
        "|".join(signal_key.split("|")[:2]),
        "safety_governor",
        policy,
        evidence,
    )
    if not inserted:
        reset_window = bool(
            not existing
            or existing.get("status") != "active"
            or existing.get("policy", {}).get("governor_mode") != mode
        )
        reset_created_at = utc_now() if reset_window else None
        conn.execute(
            """
            update signal_policies
            set created_at = coalesce(?, created_at),
                status = 'active',
                experiment_id = coalesce(experiment_id, ?),
                min_score_delta = ?,
                min_net_edge_bps = ?,
                max_spread_bps = ?,
                allocation_multiplier = ?,
                pause_entries = ?,
                expires_after_trades = null,
                policy_json = ?,
                evidence_json = ?
            where policy_id = ?
            """,
            (
                reset_created_at,
                experiment_id,
                float(policy["min_score_delta"]),
                float(policy["min_net_edge_bps"]),
                float(policy["max_spread_bps"]),
                float(policy["allocation_multiplier"]),
                1 if policy["pause_entries"] else 0,
                json.dumps(policy, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                pid,
            ),
        )
        if experiment_id:
            conn.execute(
                """
                update self_improvement_experiments
                set status = 'active',
                    activated_at = coalesce(?, activated_at),
                    decision = null,
                    completed_at = null,
                    policy_json = ?
                where id = ?
                """,
                (reset_created_at, json.dumps(policy, sort_keys=True), experiment_id),
            )
        conn.commit()

    if inserted or not existing or existing.get("status") != "active" or existing.get("policy", {}).get("governor_mode") != mode:
        add_memory_fact(
            conn,
            "signal_safety",
            signal_key,
            "activated",
            mode,
            0.86,
            "signal_safety_governor",
            {"policy_id": pid, "mode": mode, "reason": reason, "baseline": baseline, "recent": recent},
        )
    return {"signal_key": signal_key, "policy_id": pid, "mode": mode, "reason": reason, "inserted": inserted}


def run_signal_safety_governor(conn: sqlite3.Connection, settings: dict) -> dict:
    cfg = _cfg(settings)
    if not cfg.get("enabled", True):
        report = {"enabled": False, "generated_at": utc_now()}
        _write_report(report)
        return report

    activated = []
    released = []
    observed = []
    recent_limit = int(cfg["recent_trade_window"])
    stats_rows = _signal_stats(conn)

    for stats in stats_rows:
        signal_key = stats["signal_key"]
        recent = _closed_metrics(conn, signal_key, limit=recent_limit)
        mode, reason = _classify_signal(stats, recent, cfg)
        existing = _existing_governor_policy(conn, signal_key)
        if existing and existing.get("status") == "active":
            recovery = _recovery_metrics(conn, existing, signal_key)
            if _meets_release_criteria(recovery, existing):
                released.append(_release_policy(conn, existing, recovery))
                existing = None
            elif mode in {"healthy", "observe"}:
                observed.append(
                    {
                        "signal_key": signal_key,
                        "mode": existing.get("policy", {}).get("governor_mode"),
                        "status": "held_until_recovery_evidence",
                        "reason": "classification_improved_but_recovery_criteria_not_met",
                        "recovery": recovery,
                    }
                )
                continue

        if mode in {"quarantine", "probation"}:
            activated.append(_upsert_policy(conn, signal_key, mode, reason, stats, recent, settings, cfg))
        else:
            observed.append({"signal_key": signal_key, "mode": mode, "reason": reason, "recent": recent})

    active = _active_governor_policies(conn)
    report = {
        "enabled": True,
        "generated_at": utc_now(),
        "config": cfg,
        "activated_or_refreshed": activated,
        "released": released,
        "observed": observed,
        "active_governor_policies": active,
        "summary": {
            "active_count": len(active),
            "quarantine_count": sum(1 for item in active if item.get("policy", {}).get("governor_mode") == "quarantine"),
            "probation_count": sum(1 for item in active if item.get("policy", {}).get("governor_mode") == "probation"),
            "released_this_loop": len(released),
        },
    }
    _write_report(report)
    return report


def _active_governor_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select policy_id, created_at, experiment_id, signal_key, status, min_score_delta,
               min_net_edge_bps, max_spread_bps, allocation_multiplier, pause_entries,
               applied_count, filtered_count, opened_count, policy_json, evidence_json
        from signal_policies
        where policy_type = 'safety_governor' and status = 'active'
        order by pause_entries desc, signal_key asc
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["pause_entries"] = bool(item["pause_entries"])
        item["policy"] = json.loads(item.pop("policy_json") or "{}")
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        item["recovery"] = _closed_metrics(conn, item["signal_key"], since=item["created_at"])
        output.append(item)
    return output


def _write_report(report: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")


def _markdown(report: dict) -> str:
    lines = [
        "# Signal Safety Governor",
        "",
        "Persistent paper-only guardrails for signal families. Quarantined signals still receive small recovery probes so changing markets can earn release.",
        "",
        f"- Enabled: `{report.get('enabled')}`",
        f"- Generated: `{report.get('generated_at')}`",
    ]
    summary = report.get("summary", {})
    if summary:
        lines.extend(
            [
                f"- Active governor policies: `{summary.get('active_count', 0)}`",
                f"- Quarantined: `{summary.get('quarantine_count', 0)}`",
                f"- Probation: `{summary.get('probation_count', 0)}`",
                f"- Released this loop: `{summary.get('released_this_loop', 0)}`",
            ]
        )
    lines.extend(["", "## Active", ""])
    active = report.get("active_governor_policies", [])
    if not active:
        lines.append("No active governor policies.")
    for item in active:
        policy = item.get("policy", {})
        recovery = item.get("recovery", {})
        lines.append(
            f"- `{item['signal_key']}` `{policy.get('governor_mode')}` "
            f"allocation={item.get('allocation_multiplier')} applied={item.get('applied_count')} "
            f"filtered={item.get('filtered_count')} opened={item.get('opened_count')}"
        )
        lines.append(f"  - Recovery evidence: {recovery}")
        lines.append(f"  - Release criteria: {policy.get('release_criteria')}")
    lines.extend(["", "## Released", ""])
    released = report.get("released", [])
    if not released:
        lines.append("No releases this loop.")
    for item in released:
        lines.append(f"- `{item['signal_key']}` released from `{item['policy_id']}` with recovery {item['recovery']}")
    return "\n".join(lines) + "\n"
