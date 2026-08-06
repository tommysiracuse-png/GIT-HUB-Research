"""Persisted paper-only quarantine for decayed OKX basis signal families."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

POLICY_KEY = "okx_perp_funding_basis_decay_quarantine"
REASON = "decayed_basis_mean_reversion_quarantine"
REASON_ALIASES = frozenset({REASON, "decay_quarantine"})
DEFAULT_CLOSED_LABEL_LIMIT = 30
DEFAULT_DURATION_DAYS = 14
DEFAULT_MAX_LEARNED_PENALTY = 15.0
DEFAULT_PAPER_SCORE_CAP = 0.0
DEFAULT_MIN_CLOSED_COUNT = 20
DEFAULT_MAX_AVG_PNL_BPS = -25.0
DEFAULT_MAX_SCORE_ADJUSTMENT = -10.0
DEFAULT_RELEASE_MIN_AVG_PNL_BPS = 5.0
DEFAULT_RELEASE_MIN_WIN_RATE = 0.50
_LIVE_MODES = {"live", "production", "prod", "real", "broker"}
_EXEMPT_DIRECTIONS = frozenset(
    {
        "funding_capture_long_perp",
        "funding_capture_short_perp",
        "long_perp_short_spot",
        "short_perp_long_spot",
    }
)
_TARGET_DIRECTIONS = frozenset(
    {
        "basis_mean_reversion_long_perp",
        "basis_mean_reversion_short_perp",
    }
)
_CONDITIONAL_TARGET_DIRECTIONS = frozenset()
_QUARANTINE_ACTION = "quarantined_basis_mr"
_EXACT_SIGNAL_ID_FIELDS = (
    "signal_key",
    "market_key",
    "paper_context_key",
    "route_id",
    "route_registry_id",
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
        "paper_score_cap": DEFAULT_PAPER_SCORE_CAP,
        "min_closed_count": DEFAULT_MIN_CLOSED_COUNT,
        "max_avg_pnl_bps": DEFAULT_MAX_AVG_PNL_BPS,
        "max_score_adjustment": DEFAULT_MAX_SCORE_ADJUSTMENT,
        "release_min_avg_pnl_bps": DEFAULT_RELEASE_MIN_AVG_PNL_BPS,
        "release_min_win_rate": DEFAULT_RELEASE_MIN_WIN_RATE,
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
    policy["paper_score_cap"] = round(
        max(0.0, _as_float(policy.get("paper_score_cap"), DEFAULT_PAPER_SCORE_CAP)),
        3,
    )
    try:
        policy["min_closed_count"] = max(1, int(policy.get("min_closed_count", DEFAULT_MIN_CLOSED_COUNT)))
    except (TypeError, ValueError):
        policy["min_closed_count"] = DEFAULT_MIN_CLOSED_COUNT
    policy["max_avg_pnl_bps"] = round(
        _as_float(policy.get("max_avg_pnl_bps"), DEFAULT_MAX_AVG_PNL_BPS),
        3,
    )
    policy["max_score_adjustment"] = round(
        _as_float(policy.get("max_score_adjustment"), DEFAULT_MAX_SCORE_ADJUSTMENT),
        3,
    )
    policy["release_min_avg_pnl_bps"] = round(
        _as_float(policy.get("release_min_avg_pnl_bps"), DEFAULT_RELEASE_MIN_AVG_PNL_BPS),
        3,
    )
    policy["release_min_win_rate"] = round(
        min(
            1.0,
            max(0.0, _as_float(policy.get("release_min_win_rate"), DEFAULT_RELEASE_MIN_WIN_RATE)),
        ),
        6,
    )
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
    desired_mode = "zero_cap" if zero_score else "paper_score_cap"
    if (
        isinstance(existing, Mapping)
        and matches_reason(existing.get("reason"))
        and str(existing.get("mode") or "").strip() == desired_mode
    ):
        candidate.setdefault(
            "pre_okx_basis_decay_quarantine_score",
            _as_float(existing.get("pre_quarantine_score"), _as_float(candidate.get("score"), 0.0)),
        )
        candidate["score"] = round(
            _as_float(existing.get("post_quarantine_score"), _as_float(candidate.get("score"), 0.0)),
            3,
        )
        return dict(existing)

    pre_score = max(
        0.0,
        _as_float(
            candidate.get("pre_okx_basis_decay_quarantine_score"),
            _as_float(existing.get("pre_quarantine_score") if isinstance(existing, Mapping) else None, _as_float(candidate.get("score"), 0.0)),
        ),
    )
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
        score_cap = _policy(settings).get("paper_score_cap", DEFAULT_PAPER_SCORE_CAP)
        post_score = min(pre_score, _as_float(score_cap, DEFAULT_PAPER_SCORE_CAP))
        policy = {
            "reason": REASON,
            "mode": "paper_score_cap",
            "pre_quarantine_score": round(pre_score, 3),
            "post_quarantine_score": round(post_score, 3),
            "score_delta": round(post_score - pre_score, 3),
            "paper_score_cap": round(_as_float(score_cap, DEFAULT_PAPER_SCORE_CAP), 3),
        }
    candidate["pre_okx_basis_decay_quarantine_score"] = round(pre_score, 3)
    candidate["score"] = round(post_score, 3)
    candidate["okx_basis_decay_quarantine_score_policy"] = policy
    return policy


def clear_quarantine_state(
    candidate: dict[str, Any],
    *,
    restore_score: bool = True,
) -> dict[str, Any]:
    """Remove stale decay-quarantine score and block state from a reused candidate."""
    if restore_score:
        restored_score = _as_float(
            candidate.get("pre_okx_basis_decay_quarantine_score"),
            _as_float(
                (candidate.get("okx_basis_decay_quarantine_score_policy") or {}).get("pre_quarantine_score"),
                _as_float(candidate.get("score"), 0.0),
            ),
        )
        candidate["score"] = round(max(0.0, restored_score), 3)
    candidate.pop("pre_okx_basis_decay_quarantine_score", None)
    candidate.pop("okx_basis_decay_quarantine_score_policy", None)

    guard_would_block = candidate.get("paper_guard_would_block")
    if isinstance(guard_would_block, Mapping) and str(guard_would_block.get("guard") or "").strip() == POLICY_KEY:
        candidate.pop("paper_guard_would_block", None)

    reasons = [
        item
        for item in list(candidate.get("paper_exploration_would_block_reasons") or [])
        if not matches_reason(item)
    ]
    if reasons:
        candidate["paper_exploration_would_block_reasons"] = reasons
    else:
        candidate.pop("paper_exploration_would_block_reasons", None)

    if matches_reason(candidate.get("candidate_reject_reason")):
        candidate.pop("candidate_reject_reason", None)
        candidate.pop("candidate_reject_detail", None)

    if candidate.get("paper_observation_reason") == REASON:
        candidate.pop("paper_observation_reason", None)
        candidate.pop("paper_observation_only", None)
        if candidate.get("paper_execution_mode") == "observe_only":
            candidate.pop("paper_execution_mode", None)

    if candidate.get("paper_action") == "shadow_trial":
        candidate.pop("paper_action", None)
    if candidate.get("paper_action") == "shadow_only":
        candidate.pop("paper_action", None)
    if candidate.get("paper_action") == "shadow_filtered":
        candidate.pop("paper_action", None)
    if candidate.get("paper_quarantine_status") == "shadow_quarantined":
        candidate.pop("paper_quarantine_status", None)
    if candidate.get("paper_quarantine_status") == "shadow_only":
        candidate.pop("paper_quarantine_status", None)
    if candidate.get("paper_quarantine_status") == _QUARANTINE_ACTION:
        candidate.pop("paper_quarantine_status", None)
    if candidate.get("candidate_status") == "shadow_quarantined":
        candidate.pop("candidate_status", None)
    if candidate.get("candidate_status") == "shadow_only":
        candidate.pop("candidate_status", None)
    if candidate.get("candidate_status") == _QUARANTINE_ACTION:
        candidate.pop("candidate_status", None)
    if candidate.get("paper_status") == "shadow_only":
        candidate.pop("paper_status", None)
    if candidate.get("paper_status") == "shadow_filtered":
        candidate.pop("paper_status", None)
    if candidate.get("paper_fill_status") == "shadow_only":
        candidate.pop("paper_fill_status", None)
    if candidate.get("paper_fill_status") == "shadow_filtered":
        candidate.pop("paper_fill_status", None)
    if candidate.get("paper_order_status") == "shadow_only":
        candidate.pop("paper_order_status", None)
    if candidate.get("paper_order_status") == "shadow_filtered":
        candidate.pop("paper_order_status", None)
    if candidate.get("quality_action") == "shadow_only":
        candidate.pop("quality_action", None)
    if candidate.get("quality_action") == "shadow_filtered":
        candidate.pop("quality_action", None)
    if candidate.get("router_action") == _QUARANTINE_ACTION:
        candidate.pop("router_action", None)

    candidate.pop("shadow_filtered", None)
    candidate["paper_fill_allowed"] = True
    candidate["paper_eligible"] = True
    candidate["paper_entry_blocked"] = False
    candidate["paper_observation_only"] = False
    return candidate


def _feasibility_status(candidate: Mapping[str, Any]) -> str:
    # The normalized candidate field is the explicit signal-family status.
    # Prefer it over an older nested route snapshot when both are present.
    if candidate.get("feasibility_status") is not None:
        return str(candidate["feasibility_status"]).strip().lower()
    feasibility = candidate.get("execution_feasibility")
    if isinstance(feasibility, Mapping) and feasibility.get("status") is not None:
        return str(feasibility["status"]).strip().lower()
    return str(candidate.get("feasibility_status") or "unknown").strip().lower()


def _route_status(candidate: Mapping[str, Any]) -> str:
    if candidate.get("route_status") is not None:
        return str(candidate["route_status"]).strip().lower()
    feasibility = candidate.get("execution_feasibility")
    if isinstance(feasibility, Mapping) and feasibility.get("route_status") is not None:
        return str(feasibility["route_status"]).strip().lower()
    execution_route = candidate.get("execution_route")
    if isinstance(execution_route, Mapping) and execution_route.get("route_status") is not None:
        return str(execution_route["route_status"]).strip().lower()
    return _feasibility_status(candidate)


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


def _parse_exact_target_signal(raw_value: object) -> dict[str, str] | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    parts = raw.split("|")
    if len(parts) < 4:
        return None
    venue = parts[0].strip().upper()
    trade_type = parts[1].strip()
    direction = parts[2].strip()
    status = parts[3].strip().lower() or "unknown"
    if venue != "OKX" or trade_type != "perp_funding_basis":
        return None
    if not _is_target_direction(direction, status):
        return None
    return {
        "venue": venue,
        "trade_type": trade_type,
        "direction_mode": direction,
        "feasibility_status": status,
        "signal_key": f"{venue}|{trade_type}|{direction}|{status}",
    }


def _has_explicit_signal_identity(candidate: Mapping[str, Any]) -> bool:
    return any(str(candidate.get(field) or "").strip() for field in _EXACT_SIGNAL_ID_FIELDS)


def _explicit_target_signal(candidate: Mapping[str, Any]) -> dict[str, str] | None:
    for field in _EXACT_SIGNAL_ID_FIELDS:
        explicit = _parse_exact_target_signal(candidate.get(field))
        if explicit is not None:
            return explicit
    return None


def _is_target_direction(direction: str, status: str) -> bool:
    normalized_direction = str(direction or "").strip()
    normalized_status = str(status or "").strip().lower()
    if normalized_direction in _TARGET_DIRECTIONS:
        return True
    if normalized_direction in _CONDITIONAL_TARGET_DIRECTIONS:
        return normalized_status == "conditional"
    return False


def target_signal(candidate: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the exact decayed signal family identity, without prose matching."""
    if _separate_proxy_lineage(candidate):
        return None
    explicit = _explicit_target_signal(candidate)
    if explicit is not None:
        return explicit
    if _has_explicit_signal_identity(candidate) or candidate.get("strategy_lab_id") or candidate.get("signal_lineage_key"):
        return None
    signal_key = str(candidate.get("signal_key") or "")
    parts = signal_key.split("|")
    venue = str(candidate.get("venue") or (parts[0] if len(parts) >= 1 else "")).strip().upper()
    trade_type = str(candidate.get("trade_type") or (parts[1] if len(parts) >= 2 else "")).strip()
    direction = str(candidate.get("direction_mode") or candidate.get("direction") or (parts[2] if len(parts) >= 3 else "")).strip()
    status = _feasibility_status(candidate)
    route_status = _route_status(candidate)
    if direction in _CONDITIONAL_TARGET_DIRECTIONS and route_status != "unknown":
        status = route_status
    if status == "unknown" and len(parts) >= 4:
        status = parts[3].strip().lower()
    if venue != "OKX" or trade_type != "perp_funding_basis":
        return None
    if not _is_target_direction(direction, status):
        return None
    return {
        "venue": venue,
        "trade_type": trade_type,
        "direction_mode": direction,
        "feasibility_status": status,
        "signal_key": f"{venue}|{trade_type}|{direction}|{status}",
    }


def _thresholds(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "min_closed_count": int(policy.get("min_closed_count", DEFAULT_MIN_CLOSED_COUNT)),
        "max_avg_pnl_bps": round(
            _as_float(policy.get("max_avg_pnl_bps"), DEFAULT_MAX_AVG_PNL_BPS),
            3,
        ),
        "max_score_adjustment": round(
            _as_float(policy.get("max_score_adjustment"), DEFAULT_MAX_SCORE_ADJUSTMENT),
            3,
        ),
        "paper_score_cap": round(
            _as_float(policy.get("paper_score_cap"), DEFAULT_PAPER_SCORE_CAP),
            3,
        ),
        "release_min_avg_pnl_bps": round(
            _as_float(policy.get("release_min_avg_pnl_bps"), DEFAULT_RELEASE_MIN_AVG_PNL_BPS),
            3,
        ),
        "release_min_win_rate": round(
            _as_float(policy.get("release_min_win_rate"), DEFAULT_RELEASE_MIN_WIN_RATE),
            6,
        ),
    }


def _normalize_signal_stats(value: Mapping[str, Any] | None, *, expected_signal_key: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    signal_key_value = str(
        value.get("signal_key")
        or value.get("source_signal_key")
        or value.get("target_signal_key")
        or expected_signal_key
    ).strip()
    if signal_key_value != expected_signal_key:
        return None
    closed_count = int(max(0, _as_float(value.get("closed_count"), 0.0)))
    return {
        "signal_key": signal_key_value,
        "closed_count": closed_count,
        "avg_pnl_bps": round(_as_float(value.get("avg_pnl_bps"), 0.0), 3),
        "score_adjustment": round(_as_float(value.get("score_adjustment"), 0.0), 3),
        "win_rate": round(_as_float(value.get("win_rate"), 0.0), 6),
        "updated_at": value.get("updated_at"),
    }


def _candidate_signal_stats(candidate: Mapping[str, Any], *, expected_signal_key: str) -> dict[str, Any] | None:
    for field in (
        "paper_okx_basis_decay_signal_stats",
        "okx_basis_decay_signal_stats",
        "target_signal_stats",
        "signal_stats",
    ):
        normalized = _normalize_signal_stats(candidate.get(field), expected_signal_key=expected_signal_key)
        if normalized is not None:
            return normalized
    existing = candidate.get("paper_okx_basis_decay_quarantine")
    if isinstance(existing, Mapping):
        normalized = _normalize_signal_stats(existing.get("signal_stats"), expected_signal_key=expected_signal_key)
        if normalized is not None:
            return normalized
    return None


def _load_signal_stats(conn: Any | None, *, expected_signal_key: str) -> dict[str, Any] | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            select signal_key, closed_count, avg_pnl_bps, win_rate, score_adjustment, updated_at
            from signal_stats
            where signal_key = ?
            """,
            (expected_signal_key,),
        ).fetchone()
    except Exception:  # noqa: BLE001 - missing optional runtime evidence disables this guard
        return None
    if row is None:
        return None
    try:
        raw = dict(row)
    except (TypeError, ValueError):
        raw = {
            "signal_key": row[0],
            "closed_count": row[1],
            "avg_pnl_bps": row[2],
            "win_rate": row[3],
            "score_adjustment": row[4],
            "updated_at": row[5],
        }
    return _normalize_signal_stats(raw, expected_signal_key=expected_signal_key)


def _matching_signal_stats(
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any],
    conn: Any | None,
    *,
    expected_signal_key: str,
) -> dict[str, Any] | None:
    stats = _candidate_signal_stats(candidate, expected_signal_key=expected_signal_key)
    if stats is None:
        stats = _load_signal_stats(conn, expected_signal_key=expected_signal_key)
    return stats


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


def _closed_label_statistics(
    conn: Any | None,
    started_at: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if conn is None:
        return {
            "closed_label_count": 0,
            "avg_pnl_bps": None,
            "win_rate": None,
            "rolling_window_closed_label_count": 0,
            "rolling_avg_pnl_bps": None,
            "rolling_win_rate": None,
        }
    try:
        rows = conn.execute(
            """
            select direction, candidate_json, pnl_bps
            from paper_trades
            where status = 'closed' and pnl_bps is not null and closed_at >= ?
              and venue = 'OKX' and trade_type = 'perp_funding_basis'
            order by closed_at desc, id desc
            """,
            (started_at,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - unavailable evidence must not release the paper guard
        return {
            "closed_label_count": 0,
            "avg_pnl_bps": None,
            "win_rate": None,
            "rolling_window_closed_label_count": 0,
            "rolling_avg_pnl_bps": None,
            "rolling_win_rate": None,
        }
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
        if target_signal(candidate) is not None and candidate_quarantine_record(candidate) is not None:
            try:
                pnls.append(float(row["pnl_bps"]))
            except (TypeError, ValueError):
                continue
    rolling_window = max(1, int(policy.get("closed_label_limit", DEFAULT_CLOSED_LABEL_LIMIT)))
    rolling_pnls = pnls[:rolling_window]
    return {
        "closed_label_count": len(pnls),
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3) if pnls else None,
        "win_rate": round(sum(value > 0.0 for value in pnls) / len(pnls), 6) if pnls else None,
        "rolling_window_closed_label_count": len(rolling_pnls),
        "rolling_avg_pnl_bps": round(sum(rolling_pnls) / len(rolling_pnls), 3) if rolling_pnls else None,
        "rolling_win_rate": (
            round(sum(value > 0.0 for value in rolling_pnls) / len(rolling_pnls), 6)
            if rolling_pnls
            else None
        ),
    }


def _shadow_signal_stats(target: Mapping[str, Any], signal_stats: Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(signal_stats, Mapping):
        return dict(signal_stats)
    return {
        "signal_key": str(target.get("signal_key") or ""),
        "closed_count": 0,
        "avg_pnl_bps": None,
        "score_adjustment": None,
        "win_rate": None,
        "updated_at": None,
    }


def _release_reason_from_shadow_statistics(
    label_statistics: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> str | None:
    if int(label_statistics.get("rolling_window_closed_label_count") or 0) < int(
        policy.get("closed_label_limit", DEFAULT_CLOSED_LABEL_LIMIT)
    ):
        return None
    avg_pnl_bps = label_statistics.get("rolling_avg_pnl_bps")
    win_rate = label_statistics.get("rolling_win_rate")
    if avg_pnl_bps is None or win_rate is None:
        return None
    if _as_float(avg_pnl_bps, 0.0) <= _as_float(
        policy.get("release_min_avg_pnl_bps"),
        DEFAULT_RELEASE_MIN_AVG_PNL_BPS,
    ):
        return None
    if _as_float(win_rate, 0.0) <= _as_float(
        policy.get("release_min_win_rate"),
        DEFAULT_RELEASE_MIN_WIN_RATE,
    ):
        return None
    return "rolling_shadow_recovery_confirmed"


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
    signal_stats = _matching_signal_stats(
        candidate,
        policy,
        conn,
        expected_signal_key=target["signal_key"],
    )
    signal_stats = _shadow_signal_stats(target, signal_stats)
    existing = candidate.get("paper_okx_basis_decay_quarantine")
    if conn is None and isinstance(existing, Mapping):
        if not existing.get("active"):
            return None
        record = dict(existing)
        record.update(
            {
                "diagnostic_only": True,
                "would_block": True,
                "paper_fill_allowed": False,
                "shadow_filtered": True,
                "shadow_only": True,
                "quarantine_action": _QUARANTINE_ACTION,
                "signal_stats_scope": "synthetic_research",
                "paper_execution_mode": "observe_only",
                "paper_execution_semantics": "counterfactual_okx_basis_decay_guard",
                "signal_stats": signal_stats,
                "thresholds": _thresholds(policy),
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
    label_statistics = _closed_label_statistics(conn, started_at, policy)
    state["closed_label_count"] = label_statistics["closed_label_count"]
    release_reason = None
    if state.get("status") == "active":
        release_reason = _release_reason_from_shadow_statistics(label_statistics, policy)
    if release_reason:
        state["status"] = "released"
        state["release_reason"] = release_reason
    state["updated_at"] = now.isoformat()
    _persist_state(conn, state)
    active = state.get("status") == "active"
    diagnostic_only = bool(active)
    return {
        "reason": REASON,
        "guard": POLICY_KEY,
        "paper_only": True,
        "active": active,
        "diagnostic_only": diagnostic_only,
        "would_block": active,
        "paper_fill_allowed": not active,
        "shadow_filtered": active,
        "shadow_only": active,
        "quarantine_action": (
            _QUARANTINE_ACTION
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
        "rolling_window_closed_label_count": int(label_statistics["rolling_window_closed_label_count"]),
        "rolling_avg_pnl_bps": label_statistics["rolling_avg_pnl_bps"],
        "rolling_win_rate": label_statistics["rolling_win_rate"],
        "max_learned_penalty": round(maximum_learned_penalty(settings), 3),
        "paper_score_cap": round(_as_float(policy.get("paper_score_cap"), DEFAULT_PAPER_SCORE_CAP), 3),
        "signal_stats": signal_stats,
        "thresholds": _thresholds(policy),
        "signal_stats_scope": "synthetic_research",
        "paper_execution_mode": "observe_only",
        "paper_execution_semantics": "counterfactual_okx_basis_decay_guard",
        "status": state["status"],
        "release_reason": state.get("release_reason"),
    }


def apply_quarantine(
    candidate: Mapping[str, Any],
    settings: Mapping[str, Any] | bool | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Convert decayed OKX basis variants into shadow-filtered paper diagnostics."""
    guarded = dict(candidate)
    record = quarantine_record(guarded, settings, conn=conn)
    if record is None:
        return guarded
    guarded["paper_okx_basis_decay_quarantine"] = record
    if not record["active"]:
        clear_quarantine_state(guarded)
        return guarded
    clear_quarantine_state(guarded)
    score_policy = apply_score_policy(guarded, settings, zero_score=True)
    quarantine_action = str(record.get("quarantine_action") or _QUARANTINE_ACTION)
    reasons = list(guarded.get("paper_exploration_would_block_reasons") or [])
    reasons.append(REASON)
    guarded["paper_exploration_would_block_reasons"] = list(dict.fromkeys(reasons))
    guarded["paper_guard_would_block"] = {
        "reason": REASON,
        "guard": POLICY_KEY,
        "record": dict(record),
    }
    guarded.update(
        {
            "shadow_filtered": True,
            "paper_fill_allowed": False,
            "paper_eligible": False,
            "paper_action": "shadow_filtered",
            "paper_status": "shadow_filtered",
            "paper_fill_status": "shadow_filtered",
            "paper_order_status": "shadow_filtered",
            "router_action": quarantine_action,
            "paper_execution_mode": "observe_only",
            "paper_observation_only": True,
            "paper_observation_reason": REASON,
            "paper_execution_semantics": str(
                record.get("paper_execution_semantics") or "counterfactual_okx_basis_decay_guard"
            ),
            "signal_stats_scope": str(record.get("signal_stats_scope") or "synthetic_research"),
            "candidate_status": quarantine_action,
            "paper_quarantine_status": quarantine_action,
            "promotion_eligible": False,
            "paper_entry_blocked": True,
            "paper_score_eligible": False,
            "paper_rank_eligible": True,
            "paper_allocation_multiplier": 0.0,
            "quality_action": "shadow_filtered",
            "candidate_reject_reason": REASON,
            "candidate_reject_detail": record,
            "shadow_reason": REASON,
            "_hunter_bucket": "diagnose",
            "paper_okx_basis_decay_signal_stats": dict(record.get("signal_stats") or {}),
            "okx_basis_decay_quarantine_score_policy": score_policy,
        }
    )
    return guarded


def candidate_quarantine_record(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recover the structured quarantine record from persisted candidate state."""
    direct = candidate.get("paper_okx_basis_decay_quarantine")
    if isinstance(direct, Mapping):
        record = dict(direct)
    else:
        record = None

    if record is None:
        would_block = candidate.get("paper_guard_would_block")
        if isinstance(would_block, Mapping):
            nested = would_block.get("record")
            if isinstance(nested, Mapping):
                record = dict(nested)
            elif (
                str(would_block.get("guard") or "").strip() == POLICY_KEY
                or matches_reason(would_block.get("reason"))
            ):
                record = {
                    "reason": would_block.get("reason") or REASON,
                    "guard": would_block.get("guard") or POLICY_KEY,
                }

    if record is None and matches_reason(candidate.get("candidate_reject_reason")):
        detail = candidate.get("candidate_reject_detail")
        if isinstance(detail, Mapping):
            record = dict(detail)
        else:
            record = {
                "reason": candidate.get("candidate_reject_reason") or REASON,
                "guard": POLICY_KEY,
            }

    if not isinstance(record, dict) or not matches_reason(record.get("reason")):
        return None
    record.setdefault("reason", REASON)
    record.setdefault("guard", POLICY_KEY)
    record.setdefault("active", True)
    return record


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
        record = candidate_quarantine_record(candidate)
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
        record = candidate_quarantine_record(candidate)
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
    label_statistics = _closed_label_statistics(conn, str(state["started_at"]), policy)
    state["closed_label_count"] = label_statistics["closed_label_count"]
    now = dt.datetime.now(dt.timezone.utc)
    if state.get("status") == "active":
        release_reason = _release_reason_from_shadow_statistics(label_statistics, policy)
        if release_reason:
            state["status"] = "released"
            state["release_reason"] = release_reason
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
        "rolling_window_closed_label_count": int(label_statistics.get("rolling_window_closed_label_count") or 0),
        "rolling_avg_pnl_bps": label_statistics.get("rolling_avg_pnl_bps"),
        "rolling_win_rate": label_statistics.get("rolling_win_rate"),
        "release_min_avg_pnl_bps": round(
            _as_float(policy.get("release_min_avg_pnl_bps"), DEFAULT_RELEASE_MIN_AVG_PNL_BPS),
            3,
        ),
        "release_min_win_rate": round(
            _as_float(policy.get("release_min_win_rate"), DEFAULT_RELEASE_MIN_WIN_RATE),
            6,
        ),
        "release_reason": state.get("release_reason"),
        **execution_metrics,
    }
