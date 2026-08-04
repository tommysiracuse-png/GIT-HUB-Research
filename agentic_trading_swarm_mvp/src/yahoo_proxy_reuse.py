"""Paper-only validity checks for reusing Yahoo proxy momentum elsewhere."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any, Mapping


DEFAULT_PROXY_REUSE_POLICY = {
    "enabled": True,
    "max_quote_age_seconds": 20 * 60.0,
    "max_expected_bar_lag_seconds": 2 * 60.0,
    "default_bar_interval_seconds": 15 * 60.0,
    "min_opening_gap_bps": 40.0,
    "min_opening_gap_followthrough_ratio": 0.25,
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _timestamp(value: object) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        if abs(numeric) > 10_000_000_000.0:
            numeric /= 1000.0
        return numeric
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _now_timestamp(now: object) -> float:
    if isinstance(now, dt.datetime):
        normalized = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
        return normalized.timestamp()
    parsed = _timestamp(now)
    return parsed if parsed is not None else dt.datetime.now(dt.timezone.utc).timestamp()


def _policy(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = dict(DEFAULT_PROXY_REUSE_POLICY)
    configured = (settings or {}).get("yahoo_proxy_reuse_gate", {})
    if isinstance(configured, Mapping):
        policy.update(configured)
    return policy


def _bar_interval_seconds(meta: Mapping[str, Any], timestamps: list[float], policy: Mapping[str, Any]) -> float:
    granularity = str(meta.get("dataGranularity") or "").strip().lower()
    multipliers = {"m": 60.0, "h": 3600.0, "d": 86400.0}
    if len(granularity) > 1 and granularity[-1:] in multipliers:
        parsed = _number(granularity[:-1])
        if parsed and parsed > 0.0:
            return parsed * multipliers[granularity[-1]]
    gaps = [right - left for left, right in zip(timestamps, timestamps[1:]) if 0.0 < right - left <= 86400.0]
    if gaps:
        return float(statistics.median(gaps[-20:]))
    return float(policy["default_bar_interval_seconds"])


def _session(meta: Mapping[str, Any], now_ts: float) -> tuple[str, float | None, float | None]:
    market_state = str(meta.get("marketState") or "").strip().lower()
    if market_state in {"regular", "open"}:
        explicit = "open"
    elif market_state in {"closed", "post", "postpost", "pre", "halted"}:
        explicit = "closed"
    else:
        explicit = "unknown"
    regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    start = _timestamp(regular.get("start"))
    end = _timestamp(regular.get("end"))
    if start is not None and end is not None and end > start:
        return ("open" if start <= now_ts <= end else "closed"), start, end
    return explicit, start, end


def _bps_change(new: float, old: float) -> float:
    return (new / old - 1.0) * 10_000.0 if old > 0.0 else 0.0


def evaluate_yahoo_proxy_reuse(
    chart: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
    *,
    now: object = None,
) -> dict[str, Any]:
    """Return whether chart momentum may influence another paper market.

    Native Yahoo observations remain available even when this gate fails.  The
    boolean controls only cross-surface candidate creation and confirmation.
    Missing data needed by a configured check fails closed.
    """

    policy = _policy(settings)
    enabled = bool(policy.get("enabled", True))
    now_ts = _now_timestamp(now)
    meta = chart.get("meta") if isinstance(chart.get("meta"), Mapping) else {}
    raw_timestamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    valid_rows: list[tuple[float, float, float | None]] = []
    for index, raw_ts in enumerate(raw_timestamps):
        ts = _timestamp(raw_ts)
        close = _number(closes[index]) if index < len(closes) else None
        open_price = _number(opens[index]) if index < len(opens) else None
        if ts is not None and close is not None and close > 0.0:
            valid_rows.append((ts, close, open_price))

    reasons: list[str] = []
    latest_ts = valid_rows[-1][0] if valid_rows else None
    quote_age = max(0.0, now_ts - latest_ts) if latest_ts is not None else None
    max_quote_age = float(policy["max_quote_age_seconds"])
    if latest_ts is None:
        reasons.append("missing_latest_proxy_quote")
    elif quote_age is not None and quote_age > max_quote_age:
        reasons.append("proxy_quote_age_exceeded")

    session_status, session_start, session_end = _session(meta, now_ts)
    if session_status == "closed":
        reasons.append("source_session_closed")

    timestamps = [row[0] for row in valid_rows]
    interval = _bar_interval_seconds(meta, timestamps, policy)
    expected_bar_ts = None
    schedule_lag = None
    if session_status == "open" and session_start is not None and latest_ts is not None:
        elapsed = max(0.0, now_ts - session_start)
        expected_bar_ts = session_start + math.floor(elapsed / interval) * interval
        schedule_lag = max(0.0, expected_bar_ts - latest_ts)
        if schedule_lag > float(policy["max_expected_bar_lag_seconds"]):
            reasons.append("proxy_bar_schedule_lag_exceeded")

    opening_gap_bps = None
    followthrough_bps = None
    followthrough_ratio = None
    if session_start is not None and valid_rows:
        prior_rows = [row for row in valid_rows if row[0] < session_start]
        session_rows = [row for row in valid_rows if row[0] >= session_start]
        if prior_rows and session_rows:
            prior_close = prior_rows[-1][1]
            session_open = session_rows[0][2] or session_rows[0][1]
            latest_close = session_rows[-1][1]
            opening_gap_bps = _bps_change(session_open, prior_close)
            direction = 1.0 if opening_gap_bps >= 0.0 else -1.0
            followthrough_bps = direction * _bps_change(latest_close, session_open)
            if abs(opening_gap_bps) > 0.0:
                followthrough_ratio = followthrough_bps / abs(opening_gap_bps)
            if (
                abs(opening_gap_bps) >= float(policy["min_opening_gap_bps"])
                and (followthrough_ratio is None or followthrough_ratio < float(policy["min_opening_gap_followthrough_ratio"]))
            ):
                reasons.append("opening_gap_without_live_followthrough")

    valid = bool(enabled and not reasons)
    if not enabled:
        valid = True
    return {
        "proxy_valid_for_reuse": valid,
        "enabled": enabled,
        "paper_only": True,
        "reasons": reasons,
        "reason": reasons[0] if reasons else None,
        "source_session_status": session_status,
        "source_session_start_utc": dt.datetime.fromtimestamp(session_start, dt.timezone.utc).isoformat() if session_start is not None else None,
        "source_session_end_utc": dt.datetime.fromtimestamp(session_end, dt.timezone.utc).isoformat() if session_end is not None else None,
        "latest_bar_timestamp_utc": dt.datetime.fromtimestamp(latest_ts, dt.timezone.utc).isoformat() if latest_ts is not None else None,
        "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
        "max_quote_age_seconds": max_quote_age,
        "bar_interval_seconds": interval,
        "expected_bar_timestamp_utc": dt.datetime.fromtimestamp(expected_bar_ts, dt.timezone.utc).isoformat() if expected_bar_ts is not None else None,
        "expected_bar_lag_seconds": round(schedule_lag, 3) if schedule_lag is not None else None,
        "max_expected_bar_lag_seconds": float(policy["max_expected_bar_lag_seconds"]),
        "opening_gap_bps": round(opening_gap_bps, 3) if opening_gap_bps is not None else None,
        "live_session_followthrough_bps": round(followthrough_bps, 3) if followthrough_bps is not None else None,
        "opening_gap_followthrough_ratio": round(followthrough_ratio, 6) if followthrough_ratio is not None else None,
        "min_opening_gap_bps": float(policy["min_opening_gap_bps"]),
        "min_opening_gap_followthrough_ratio": float(policy["min_opening_gap_followthrough_ratio"]),
    }
