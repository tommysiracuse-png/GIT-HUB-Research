"""Persisted paper-only quarantine for decayed OKX basis signal families."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

from paper_exploration import exploration_enabled


POLICY_KEY = "okx_perp_funding_basis_decay_quarantine"
REASON = "decayed_basis_mean_reversion_quarantine"
REASON_ALIASES = frozenset({REASON, "decay_quarantine"})
DEFAULT_CLOSED_LABEL_LIMIT = 100
DEFAULT_DURATION_DAYS = 14
DEFAULT_MAX_LEARNED_PENALTY = 15.0
_LIVE_MODES = {"live", "production", "prod", "real", "broker"}
_TARGET_DIRECTIONS = frozenset(
    {
        "basis_mean_reversion_long_perp",
        "basis_mean_reversion_short_perp",
    }
)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _policy(settings: Mapping[str, Any] | bool | None) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "enabled": True,
        "closed_label_limit": DEFAULT_CLOSED_LABEL_LIMIT,
        "duration_days": DEFAULT_DURATION_DAYS,
    }
    if isinstance(settings, bool):
        policy["enabled"] = settings
        return policy
    if not isinstance(settings, Mapping):
        return policy
    for container in (
        settings,
        settings.get("paper"),
        settings.get("paper_policy"),
        settings.get("strategy_reliability"),
        settings.get("paper_exploration"),
    ):
        if not isinstance(container, Mapping):
            continue
        scoped = container.get(POLICY_KEY)
        if isinstance(scoped, Mapping):
            policy.update(scoped)
        elif scoped is not None:
            policy["enabled"] = scoped
    policy["enabled"] = _as_bool(policy.get("enabled"), True)
    try:
        policy["closed_label_limit"] = max(1, int(policy.get("closed_label_limit", DEFAULT_CLOSED_LABEL_LIMIT)))
    except (TypeError, ValueError):
        policy["closed_label_limit"] = DEFAULT_CLOSED_LABEL_LIMIT
    try:
        policy["duration_days"] = max(1, int(policy.get("duration_days", DEFAULT_DURATION_DAYS)))
    except (TypeError, ValueError):
        policy["duration_days"] = DEFAULT_DURATION_DAYS
    return policy


def _paper_context(settings: Mapping[str, Any] | bool | None) -> bool:
    if isinstance(settings, bool) or not isinstance(settings, Mapping):
        return True
    modes: list[str] = []
    for container in (settings, settings.get("paper"), settings.get("paper_policy"), settings.get("paper_exploration")):
        if not isinstance(container, Mapping):
            continue
        for field in ("mode", "runtime_mode", "execution_mode", "trading_mode"):
            value = str(container.get(field) or "").strip().lower().replace("-", "_").replace(" ", "_")
            if value:
                modes.append(value)
    return not any(mode in _LIVE_MODES for mode in modes)


def matches_reason(value: object) -> bool:
    return str(value or "").strip() in REASON_ALIASES


def maximum_learned_penalty(settings: Mapping[str, Any] | bool | None = None) -> float:
    if isinstance(settings, Mapping):
        learning = settings.get("learning")
        if isinstance(learning, Mapping):
            return max(
                0.0,
                _as_float(learning.get("max_adjustment_bps"), DEFAULT_MAX_LEARNED_PENALTY),
            )
    return DEFAULT_MAX_LEARNED_PENALTY


def apply_score_policy(
    candidate: dict[str, Any],
    settings: Mapping[str, Any] | bool | None = None,
    *,
    zero_score: bool,
) -> dict[str, Any]:
    existing = candidate.get("okx_basis_decay_quarantine_score_policy")
    if isinstance(existing, Mapping):
        candidate.setdefault(
            "pre_okx_basis_decay_quarantine_score",
            _as_float(existing.get("pre_quarantine_score"), _as_float(candidate.get("score"), 0.0)),
        )
        return dict(existing)

    pre_score = max(0.0, _as_float(candidate.get("score"), 0.0))
    if zero_score:
        post_score = 0.0
        policy = {
            "reason": REASON,
            "mode": "zero_cap",
            "pre_quarantine_score": round(pre_score, 3),
            "post_quarantine_score": 0.0,
            "score_delta": round(-pre_score, 3),
            "max_learned_penalty": maximum_learned_penalty(settings),
        }
    else:
        penalty = maximum_learned_penalty(settings)
        post_score = max(0.0, pre_score - penalty)
        policy = {
            "reason": REASON,
            "mode": "max_learned_penalty",
            "pre_quarantine_score": round(pre_score, 3),
            "post_quarantine_score": round(post_score, 3),
            "score_delta": round(post_score - pre_score, 3),
            "max_learned_penalty": round(penalty, 3),
        }
    candidate["pre_okx_basis_decay_quarantine_score"] = round(pre_score, 3)
    candidate["score"] = round(post_score, 3)
    candidate["okx_basis_decay_quarantine_score_policy"] = policy
    return policy


def _feasibility_status(candidate: Mapping[str, Any]) -> str:
    # The normalized candidate field is the explicit signal-family status.
    # Prefer it over an older nested route snapshot when both are present.
    if candidate.get("feasibility_status") is not None:
        return str(candidate["feasibility_status"]).strip().lower()
    feasibility = candidate.get("execution_feasibility")
    if isinstance(feasibility, Mapping) and feasibility.get("status") is not None:
        return str(feasibility["status"]).strip().lower()
    return str(candidate.get("feasibility_status") or "unknown").strip().lower()


def _separate_proxy_lineage(candidate: Mapping[str, Any]) -> bool:
    signal_key = str(candidate.get("signal_key") or "").strip().upper()
    stats_scope = str(candidate.get("signal_stats_scope") or "").strip().lower()
    semantics = str(
        candidate.get("paper_execution_semantics")
        or candidate.get("execution_semantics")
        or ""
    ).strip().lower()
    return bool(
        signal_key.startswith("PAPER_PROXY|")
        or stats_scope == "paper_proxy"
        or candidate.get("paper_proxy_activated")
        or candidate.get("paper_proxy_not_live_equivalent")
        or semantics == "proxy_not_live_equivalent"
    )


def _explicit_target_signal(candidate: Mapping[str, Any]) -> dict[str, str] | None:
    signal_key = str(candidate.get("signal_key") or "").strip()
    if not signal_key:
        return None
    parts = signal_key.split("|")
    if len(parts) < 4:
        return None
    venue = parts[0].strip().upper()
    trade_type = parts[1].strip()
    direction = parts[2].strip()
    status = parts[3].strip().lower() or "unknown"
    if venue != "OKX" or trade_type != "perp_funding_basis":
        return None
    if direction not in _TARGET_DIRECTIONS:
        return None
    return {
        "venue": venue,
        "trade_type": trade_type,
        "direction_mode": direction,
        "feasibility_status": status,
        "signal_key": f"{venue}|{trade_type}|{direction}|{status}",
    }


def target_signal(candidate: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the exact decayed signal family identity, without prose matching."""
    if _separate_proxy_lineage(candidate):
        return None
    explicit = _explicit_target_signal(candidate)
    if explicit is not None:
        return explicit
    if candidate.get("signal_key") or candidate.get("strategy_lab_id") or candidate.get("signal_lineage_key"):
        return None
    signal_key = str(candidate.get("signal_key") or "")
    parts = signal_key.split("|")
    venue = str(candidate.get("venue") or (parts[0] if len(parts) >= 1 else "")).strip().upper()
    trade_type = str(candidate.get("trade_type") or (parts[1] if len(parts) >= 2 else "")).strip()
    direction = str(candidate.get("direction_mode") or candidate.get("direction") or (parts[2] if len(parts) >= 3 else "")).strip()
    status = _feasibility_status(candidate)
    if status == "unknown" and len(parts) >= 4:
        status = parts[3].strip().lower()
    if venue != "OKX" or trade_type != "perp_funding_basis":
        return None
    if direction not in _TARGET_DIRECTIONS:
        return None
    return {
        "venue": venue,
        "trade_type": trade_type,
        "direction_mode": direction,
        "feasibility_status": status,
        "signal_key": f"{venue}|{trade_type}|{direction}|{status}",
    }


def _parse_time(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _load_candidate_json(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _load_state(conn: Any | None) -> dict[str, Any] | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "select policy_key, status, started_at, expires_at, closed_label_limit, "
            "closed_label_count, release_reason, updated_at from paper_decay_quarantines where policy_key = ?",
            (POLICY_KEY,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - callers still keep a conservative in-memory guard
        return None
    return dict(row) if row is not None else None


def _closed_label_statistics(conn: Any | None, started_at: str) -> dict[str, Any]:
    if conn is None:
        return {"closed_label_count": 0, "avg_pnl_bps": None, "win_rate": None}
    try:
        rows = conn.execute(
            """
            select direction, candidate_json, pnl_bps
            from paper_trades
            where status = 'closed' and pnl_bps is not null and closed_at >= ?
              and venue = 'OKX' and trade_type = 'perp_funding_basis'
            """,
            (started_at,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - unavailable evidence must not release the paper guard
        return {"closed_label_count": 0, "avg_pnl_bps": None, "win_rate": None}
    pnls: list[float] = []
    for row in rows:
        try:
            candidate = json.loads(row["candidate_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = {}
        if not isinstance(candidate, Mapping):
            candidate = {}
        candidate = {
            **candidate,
            "venue": candidate.get("venue") or "OKX",
            "trade_type": candidate.get("trade_type") or "perp_funding_basis",
            "direction": candidate.get("direction") or row["direction"],
        }
        if target_signal(candidate) is not None:
            try:
                pnls.append(float(row["pnl_bps"]))
            except (TypeError, ValueError):
                continue
    return {
        "closed_label_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3) if pnls else None,
        "win_rate": round(sum(value > 0.0 for value in pnls) / len(pnls), 6) if pnls else None,
    }


def _persist_state(conn: Any | None, state: Mapping[str, Any]) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            """
            insert into paper_decay_quarantines (
                policy_key, status, started_at, expires_at, closed_label_limit,
                closed_label_count, release_reason, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(policy_key) do update set
                status = excluded.status, closed_label_count = excluded.closed_label_count,
                release_reason = excluded.release_reason, updated_at = excluded.updated_at
            """,
            (
                POLICY_KEY,
                state["status"],
                state["started_at"],
                state["expires_at"],
                state["closed_label_limit"],
                state["closed_label_count"],
                state.get("release_reason"),
                state["updated_at"],
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - persistence failure must not weaken the in-memory gate
        return


def quarantine_record(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict[str, Any] | None:
    """Return the active paper-only quarantine record for an exact target family."""
    target = target_signal(candidate)
    policy = _policy(settings)
    if target is None or not policy["enabled"] or not _paper_context(settings):
        return None
    existing = candidate.get("paper_okx_basis_decay_quarantine")
    if conn is None and isinstance(existing, Mapping):
        if not existing.get("active"):
            return None
        record = dict(existing)
        diagnostic_only = exploration_enabled(settings if isinstance(settings, Mapping) else None)
        record.update(
            {
                "diagnostic_only": diagnostic_only,
                "would_block": True,
                "paper_fill_allowed": diagnostic_only,
                "quarantine_action": (
                    "would_block_diagnostic" if diagnostic_only else "shadow_trial_observe_only"
                ),
            }
        )
        return record

    now = dt.datetime.now(dt.timezone.utc)
    state = _load_state(conn)
    if state is None:
        started_at = now
        state = {
            "policy_key": POLICY_KEY,
            "status": "active",
            "started_at": started_at.isoformat(),
            "expires_at": (started_at + dt.timedelta(days=policy["duration_days"])).isoformat(),
            "closed_label_limit": policy["closed_label_limit"],
            "closed_label_count": 0,
            "release_reason": None,
            "updated_at": now.isoformat(),
        }
    else:
        state = dict(state)
        state.setdefault("closed_label_limit", policy["closed_label_limit"])

    started_at = str(state["started_at"])
    label_statistics = _closed_label_statistics(conn, started_at)
    state["closed_label_count"] = label_statistics["closed_label_count"]
    expires_at = _parse_time(state.get("expires_at"))
    release_reason = None
    if state.get("status") == "active":
        if state["closed_label_count"] >= int(state["closed_label_limit"]):
            release_reason = "closed_label_limit_reached"
        elif expires_at is not None and now >= expires_at:
            release_reason = "duration_elapsed"
    if release_reason:
        state["status"] = "released"
        state["release_reason"] = release_reason
    state["updated_at"] = now.isoformat()
    _persist_state(conn, state)
    active = state.get("status") == "active"
    diagnostic_only = bool(
        active and exploration_enabled(settings if isinstance(settings, Mapping) else None)
    )
    return {
        "reason": REASON,
        "guard": POLICY_KEY,
        "paper_only": True,
        "active": active,
        "diagnostic_only": diagnostic_only,
        "would_block": active,
        "paper_fill_allowed": not active or diagnostic_only,
        "quarantine_action": (
            "would_block_diagnostic"
            if diagnostic_only
            else "shadow_trial_observe_only"
            if active
            else "released"
        ),
        "target": target,
        "started_at": state["started_at"],
        "expires_at": state["expires_at"],
        "closed_label_count": int(state["closed_label_count"]),
        "closed_label_limit": int(state["closed_label_limit"]),
        "closed_label_remaining": max(0, int(state["closed_label_limit"]) - int(state["closed_label_count"])),
        "avg_pnl_bps": label_statistics["avg_pnl_bps"],
        "win_rate": label_statistics["win_rate"],
        "max_learned_penalty": round(maximum_learned_penalty(settings), 3),
        "status": state["status"],
        "release_reason": state.get("release_reason"),
    }


def apply_quarantine(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Annotate decay evidence without suppressing exploration-mode paper tests."""
    guarded = dict(candidate)
    record = quarantine_record(guarded, settings, conn=conn)
    if record is None:
        return guarded
    guarded["paper_okx_basis_decay_quarantine"] = record
    if not record["active"]:
        return guarded
    if record.get("diagnostic_only"):
        apply_score_policy(guarded, settings, zero_score=False)
        reasons = list(guarded.get("paper_exploration_would_block_reasons") or [])
        reasons.append(REASON)
        guarded["paper_exploration_would_block_reasons"] = list(dict.fromkeys(reasons))
        guarded["paper_guard_would_block"] = {
            "reason": REASON,
            "guard": POLICY_KEY,
            "record": dict(record),
        }
        guarded["candidate_status"] = "shadow_quarantined"
        guarded["paper_quarantine_status"] = "shadow_quarantined"
        guarded["promotion_eligible"] = False
        guarded["_hunter_bucket"] = "diagnose"
        if not guarded.get("paper_exploration_immutable_rejections") and not guarded.get(
            "paper_experiment_capacity_deferred"
        ):
            guarded["shadow_filtered"] = False
            guarded["paper_fill_allowed"] = True
            guarded["paper_eligible"] = True
            guarded["paper_entry_blocked"] = False
            guarded.pop("candidate_reject_reason", None)
            guarded.pop("candidate_reject_detail", None)
        return guarded
    apply_score_policy(guarded, settings, zero_score=True)
    guarded.update(
        {
            "shadow_filtered": True,
            "paper_fill_allowed": False,
            "paper_eligible": False,
            "paper_action": "shadow_trial",
            "paper_execution_mode": "observe_only",
            "paper_observation_only": True,
            "paper_observation_reason": REASON,
            "candidate_status": "shadow_quarantined",
            "paper_quarantine_status": "shadow_quarantined",
            "promotion_eligible": False,
            "candidate_reject_reason": REASON,
            "candidate_reject_detail": record,
        }
    )
    return guarded


def _execution_metrics(conn: Any | None) -> dict[str, Any]:
    metrics = {
        "quarantined_count": 0,
        "would_have_filled_count": 0,
        "shadow_valid_outcome_count": 0,
        "shadow_avg_pnl_bps": None,
        "shadow_pnl_bps": None,
    }
    if conn is None:
        return metrics
    try:
        order_rows = conn.execute(
            "select status, candidate_json from execution_orders"
        ).fetchall()
    except Exception:  # noqa: BLE001 - reporting must tolerate partial schemas
        order_rows = []
    for row in order_rows:
        candidate = _load_candidate_json(row["candidate_json"])
        record = candidate.get("paper_okx_basis_decay_quarantine")
        if (
            not isinstance(record, Mapping)
            or not bool(record.get("active"))
            or not matches_reason(record.get("reason"))
            or target_signal(candidate) is None
        ):
            continue
        metrics["quarantined_count"] += 1
        if str(row["status"] or "").strip().lower() == "paper_filled":
            metrics["would_have_filled_count"] += 1
    try:
        outcome_rows = conn.execute(
            """
            select o.measurement_status, o.pnl_bps, t.candidate_json
            from paper_trade_outcomes o
            join paper_trades t on t.id = o.trade_id
            """
        ).fetchall()
    except Exception:  # noqa: BLE001 - reporting must tolerate partial schemas
        outcome_rows = []
    pnls: list[float] = []
    for row in outcome_rows:
        candidate = _load_candidate_json(row["candidate_json"])
        record = candidate.get("paper_okx_basis_decay_quarantine")
        if (
            not isinstance(record, Mapping)
            or not bool(record.get("active"))
            or not bool(record.get("diagnostic_only"))
            or not matches_reason(record.get("reason"))
            or target_signal(candidate) is None
            or str(row["measurement_status"] or "").strip().lower() != "valid"
            or row["pnl_bps"] is None
        ):
            continue
        try:
            pnls.append(float(row["pnl_bps"]))
        except (TypeError, ValueError):
            continue
    metrics["shadow_valid_outcome_count"] = len(pnls)
    metrics["shadow_avg_pnl_bps"] = round(sum(pnls) / len(pnls), 3) if pnls else None
    metrics["shadow_pnl_bps"] = round(sum(pnls), 3) if pnls else None
    return metrics


def runtime_report(conn: Any | None, settings: Mapping[str, Any] | bool | None = None) -> dict[str, Any]:
    """Expose persisted closed-label progress without activating a new quarantine."""
    policy = _policy(settings)
    state = _load_state(conn)
    execution_metrics = _execution_metrics(conn)
    if state is None:
        return {
            "enabled": bool(policy["enabled"] and _paper_context(settings)),
            "status": "not_started",
            "reason": REASON,
            "closed_label_count": 0,
            "closed_label_limit": int(policy["closed_label_limit"]),
            "closed_label_remaining": int(policy["closed_label_limit"]),
            "expires_at": None,
            "release_reason": None,
            **execution_metrics,
        }
    state = dict(state)
    label_statistics = _closed_label_statistics(conn, str(state["started_at"]))
    state["closed_label_count"] = label_statistics["closed_label_count"]
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = _parse_time(state.get("expires_at"))
    if state.get("status") == "active" and state["closed_label_count"] >= int(state["closed_label_limit"]):
        state["status"] = "released"
        state["release_reason"] = "closed_label_limit_reached"
    elif state.get("status") == "active" and expires_at is not None and now >= expires_at:
        state["status"] = "released"
        state["release_reason"] = "duration_elapsed"
    state["updated_at"] = now.isoformat()
    _persist_state(conn, state)
    return {
        "enabled": bool(policy["enabled"] and _paper_context(settings)),
        "status": state.get("status"),
        "reason": REASON,
        "started_at": state.get("started_at"),
        "expires_at": state.get("expires_at"),
        "closed_label_count": int(state.get("closed_label_count") or 0),
        "closed_label_limit": int(state.get("closed_label_limit") or policy["closed_label_limit"]),
        "closed_label_remaining": max(0, int(state.get("closed_label_limit") or policy["closed_label_limit"]) - int(state.get("closed_label_count") or 0)),
        "avg_pnl_bps": label_statistics["avg_pnl_bps"],
        "win_rate": label_statistics["win_rate"],
        "release_reason": state.get("release_reason"),
        **execution_metrics,
    }
