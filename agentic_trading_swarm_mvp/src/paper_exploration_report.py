"""Reporting for exploration admissions and counterfactual paper guards."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter, defaultdict

from storage import RUNS_DIR, unresolved_route_requirement_shadow_summary
from paper_decay_quarantine import (
    REASON as OKX_BASIS_DECAY_QUARANTINE_REASON,
    candidate_quarantine_record as okx_basis_decay_candidate_record,
    matches_reason as okx_basis_decay_matches_reason,
    runtime_report as okx_basis_decay_quarantine_runtime_report,
    target_signal as okx_basis_decay_target_signal,
)


_GUARD_REASON_PATTERNS = (
    (
        re.compile(r"^(decay_quarantine|decayed_basis_mean_reversion_quarantine)$", re.I),
        OKX_BASIS_DECAY_QUARANTINE_REASON,
    ),
    (re.compile(r"^learned score below threshold", re.I), "learned score below threshold"),
    (re.compile(r"^estimated net edge too small after costs", re.I), "estimated net edge too small after costs"),
    (re.compile(r"^liquidity score below minimum", re.I), "liquidity score below minimum"),
    (re.compile(r"^self-improvement min learned score", re.I), "self-improvement minimum learned score not met"),
    (re.compile(r"^self-improvement min net edge", re.I), "self-improvement minimum net edge not met"),
    (re.compile(r"^self-improvement max spread", re.I), "self-improvement maximum spread exceeded"),
    (re.compile(r"^paper context cost floor not cleared", re.I), "paper context cost floor not cleared"),
    (re.compile(r"^market data dangerously stale", re.I), "market data dangerously stale"),
)

_FRONTIER_SHORT_DIAGNOSTIC_EVIDENCE = [
    "Observed issue: frontier spot short outcomes are consistently worse than nearby long/proxy contexts.",
    "Hypothesis driver 1: thin or segmented spot venues can show stale or delayed reference prices, making measured premiums non-actionable.",
    "Hypothesis driver 2: local inventory pressure can create persistent one-way premiums that continue rather than mean-revert.",
    "Hypothesis driver 3: synthetic paper short routing introduces extra basis and cost error relative to spot long/proxy contexts.",
    "Implication: the same cross-venue premium may encode different microstructure states across frontier spot, nearby spot, and proxy markets.",
]
_MAJOR_REFERENCE_QUOTES = frozenset({"USD", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "EUR", "GBP"})


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


def _maybe_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None else None


def _safe_avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bucket_rate(matches: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(float(matches) / float(total), 3)


def _premium_field(candidate: dict, review: dict) -> tuple[str | None, float | None]:
    fields = (
        "premium_vs_reference_bps",
        "premium_bps",
        "perp_premium_bps",
        "venue_deviation_bps",
        "basis_bps",
    )
    for field in fields:
        value = _maybe_float(candidate.get(field))
        if value is None:
            value = _maybe_float(review.get(field))
        if value is not None:
            return field, value
    return None, None


def _freshness_age_seconds(candidate: dict, review: dict) -> float | None:
    for field in ("freshness_age_seconds", "data_age_seconds"):
        value = _maybe_float(candidate.get(field))
        if value is not None:
            return max(0.0, value)
    minutes = _maybe_float(candidate.get("stale_minutes"))
    if minutes is not None:
        return max(0.0, minutes) * 60.0
    minutes = _maybe_float(candidate.get("freshness_minutes"))
    if minutes is not None:
        return max(0.0, minutes) * 60.0
    minutes = _maybe_float(review.get("freshness_minutes"))
    if minutes is not None:
        return max(0.0, minutes) * 60.0
    return None


def _freshness_bucket(candidate: dict, review: dict) -> str:
    state = str(candidate.get("freshness_state") or candidate.get("data_freshness_state") or "").strip().lower()
    age_seconds = _freshness_age_seconds(candidate, review)
    if state == "stale":
        return "stale"
    if age_seconds is None:
        return "unknown"
    if age_seconds <= 30.0:
        return "fresh"
    if age_seconds <= 180.0:
        return "aging"
    return "stale"


def _liquidity_bucket(candidate: dict) -> str:
    liquidity = _maybe_float(candidate.get("liquidity_score"))
    if liquidity is None:
        return "unknown"
    if liquidity < 0.35:
        return "thin"
    if liquidity < 0.65:
        return "medium"
    return "deep"


def _regional_segmentation_bucket(candidate: dict, context: dict) -> str:
    scope = str(candidate.get("signal_stats_scope") or context.get("signal_stats_scope") or "").strip().lower()
    if scope == "paper_proxy":
        return "proxy_control"
    quote = str(candidate.get("quote") or "").strip().upper()
    region = str(candidate.get("region") or context.get("region") or "unknown").strip() or "unknown"
    normalization = str(candidate.get("quote_normalization_status") or "").strip().lower()
    if normalization == "external_fx_reference":
        return "regional_fx_segmented"
    if quote and quote not in _MAJOR_REFERENCE_QUOTES:
        return "regional_fiat_segmented"
    if region.lower() not in {"", "unknown", "global"}:
        return f"regional_{region.lower().replace(' ', '_')}"
    return "integrated_major_quote"


def _synthetic_short_cost_penalty_bps(
    candidate: dict,
    *,
    frontier_short: bool,
    freshness_bucket: str,
    liquidity_bucket: str,
    regional_segmentation: str,
) -> float:
    if not frontier_short:
        return 0.0
    penalty = 4.0
    scope = str(candidate.get("signal_stats_scope") or "").strip().lower()
    if scope == "synthetic_research" or candidate.get("synthetic_research_paper"):
        penalty += 8.0
    if freshness_bucket == "aging":
        penalty += 3.0
    elif freshness_bucket == "stale":
        penalty += 8.0
    if liquidity_bucket == "medium":
        penalty += 2.0
    elif liquidity_bucket == "thin":
        penalty += 5.0
    if regional_segmentation.startswith("regional_"):
        penalty += 3.0
    return round(penalty, 3)


def _basis_cost_bucket(candidate: dict, synthetic_penalty_bps: float) -> str:
    basis_value = abs(
        _maybe_float(candidate.get("basis_bps"))
        or _maybe_float(candidate.get("premium_vs_reference_bps"))
        or 0.0
    )
    if basis_value >= 25.0 or synthetic_penalty_bps >= 14.0:
        return "high_basis"
    if basis_value >= 10.0 or synthetic_penalty_bps >= 8.0:
        return "elevated_basis"
    return "low_basis"


def _strict_stale_filter_pass(candidate: dict, review: dict) -> bool:
    bucket = _freshness_bucket(candidate, review)
    if bucket == "stale":
        return False
    age_seconds = _freshness_age_seconds(candidate, review)
    return age_seconds is None or age_seconds <= 90.0


def _direction_side(direction: str) -> int | None:
    text = str(direction or "").strip().lower()
    if "short" in text:
        return -1
    if "long" in text:
        return 1
    return None


def _premium_regime(direction: str, premium_bps: float | None, pnl_bps: float | None) -> str:
    if premium_bps is None or pnl_bps is None or premium_bps == 0.0 or pnl_bps == 0.0:
        return "neutral_or_unpriced"
    side = _direction_side(direction)
    if side is None:
        return "neutral_or_unpriced"
    expected_pnl_sign = side * (-1 if premium_bps > 0 else 1)
    actual_pnl_sign = 1 if pnl_bps > 0 else -1
    return "mean_reversion" if actual_pnl_sign == expected_pnl_sign else "momentum_continuation"


def _frontier_short_diagnostic_cohort(candidate: dict, context: dict) -> str | None:
    trade_type = str(candidate.get("trade_type") or "").strip().lower()
    direction = str(candidate.get("direction") or "").strip().lower()
    scope = str(candidate.get("signal_stats_scope") or context.get("signal_stats_scope") or "").strip().lower()
    if trade_type == "frontier_crypto_venue_map" and direction == "short_frontier_spot":
        return "frontier_short"
    if scope == "paper_proxy" or "proxy" in direction:
        return "nearby_control"
    if trade_type == "frontier_crypto_venue_map" and direction == "long_frontier_spot":
        return "nearby_control"
    return None


def _assign_premium_deciles(rows: list[dict]) -> None:
    priced = sorted(
        (
            (index, float(row["premium_abs_bps"]))
            for index, row in enumerate(rows)
            if row.get("premium_abs_bps") is not None
        ),
        key=lambda item: item[1],
    )
    total = len(priced)
    if total <= 0:
        return
    for rank, (index, _value) in enumerate(priced):
        decile = min(10, int((rank * 10) / total) + 1)
        rows[index]["premium_abs_decile"] = decile


def _subset_summary(rows: list[dict], dimension: str) -> list[dict]:
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[str(row.get(dimension) or "unknown")][row["cohort"]].append(row)
    output: list[dict] = []
    for bucket, cohorts in grouped.items():
        frontier = cohorts.get("frontier_short", [])
        control = cohorts.get("nearby_control", [])
        frontier_pnls = [float(item["pnl_bps"]) for item in frontier if item.get("pnl_bps") is not None]
        control_pnls = [float(item["pnl_bps"]) for item in control if item.get("pnl_bps") is not None]
        output.append(
            {
                "bucket": bucket,
                "frontier_short_count": len(frontier),
                "frontier_short_valid_outcome_count": len(frontier_pnls),
                "frontier_short_avg_pnl_bps": _round_or_none(_safe_avg(frontier_pnls)),
                "control_count": len(control),
                "control_valid_outcome_count": len(control_pnls),
                "control_avg_pnl_bps": _round_or_none(_safe_avg(control_pnls)),
                "underperformance_vs_control_bps": _round_or_none(
                    (_safe_avg(frontier_pnls) or 0.0) - (_safe_avg(control_pnls) or 0.0)
                ) if frontier_pnls and control_pnls else None,
            }
        )
    output.sort(
        key=lambda item: (
            item["underperformance_vs_control_bps"] is None,
            item["underperformance_vs_control_bps"] if item["underperformance_vs_control_bps"] is not None else 0.0,
            item["bucket"],
        )
    )
    return output


def _premium_decile_summary(rows: list[dict]) -> list[dict]:
    output: list[dict] = []
    for decile in range(1, 11):
        decile_rows = [row for row in rows if row.get("premium_abs_decile") == decile]
        if not decile_rows:
            continue
        frontier = [row for row in decile_rows if row["cohort"] == "frontier_short"]
        control = [row for row in decile_rows if row["cohort"] == "nearby_control"]
        frontier_pnls = [float(row["pnl_bps"]) for row in frontier if row.get("pnl_bps") is not None]
        control_pnls = [float(row["pnl_bps"]) for row in control if row.get("pnl_bps") is not None]
        frontier_regimes = Counter(
            str(row.get("premium_regime") or "neutral_or_unpriced")
            for row in frontier
            if row.get("premium_regime")
        )
        output.append(
            {
                "premium_abs_decile": decile,
                "frontier_short_count": len(frontier),
                "frontier_short_valid_outcome_count": len(frontier_pnls),
                "frontier_short_avg_pnl_bps": _round_or_none(_safe_avg(frontier_pnls)),
                "control_count": len(control),
                "control_valid_outcome_count": len(control_pnls),
                "control_avg_pnl_bps": _round_or_none(_safe_avg(control_pnls)),
                "frontier_short_mean_reversion_rate": _bucket_rate(
                    frontier_regimes.get("mean_reversion", 0),
                    len(frontier),
                ),
                "frontier_short_continuation_rate": _bucket_rate(
                    frontier_regimes.get("momentum_continuation", 0),
                    len(frontier),
                ),
                "underperformance_vs_control_bps": _round_or_none(
                    (_safe_avg(frontier_pnls) or 0.0) - (_safe_avg(control_pnls) or 0.0)
                ) if frontier_pnls and control_pnls else None,
            }
        )
    return output


def _frontier_short_diagnostic_report(rows: list[sqlite3.Row], horizon: int) -> dict:
    diagnostic_rows: list[dict] = []
    for row in rows:
        candidate = _load_json(row["candidate_json"])
        review = _load_json(row["review_json"])
        context = _load_json(row["context_json"])
        cohort = _frontier_short_diagnostic_cohort(candidate, context)
        if cohort is None:
            continue
        premium_field, premium_bps = _premium_field(candidate, review)
        freshness_bucket = _freshness_bucket(candidate, review)
        liquidity_bucket = _liquidity_bucket(candidate)
        regional_segmentation = _regional_segmentation_bucket(candidate, context)
        pnl_bps = None
        if row["measurement_status"] == "valid" and row["horizon_pnl_bps"] is not None:
            pnl_bps = float(row["horizon_pnl_bps"])
        elif row["pnl_bps"] is not None:
            pnl_bps = float(row["pnl_bps"])
        base_edge_bps = (
            _maybe_float(candidate.get("net_edge_bps"))
            or _maybe_float(review.get("net_edge_bps_estimate"))
            or _maybe_float(candidate.get("edge_bps_estimate"))
            or _maybe_float(candidate.get("venue_deviation_bps"))
        )
        synthetic_penalty_bps = _synthetic_short_cost_penalty_bps(
            candidate,
            frontier_short=cohort == "frontier_short",
            freshness_bucket=freshness_bucket,
            liquidity_bucket=liquidity_bucket,
            regional_segmentation=regional_segmentation,
        )
        strict_stale_pass = _strict_stale_filter_pass(candidate, review)
        recomputed_edge_bps = None
        if base_edge_bps is not None and strict_stale_pass:
            recomputed_edge_bps = float(base_edge_bps) - synthetic_penalty_bps
        direction = str(candidate.get("direction") or "")
        diagnostic_rows.append(
            {
                "cohort": cohort,
                "venue": str(candidate.get("venue") or "unknown"),
                "region": str(candidate.get("region") or context.get("region") or "unknown"),
                "direction": direction,
                "signal_stats_scope": str(candidate.get("signal_stats_scope") or context.get("signal_stats_scope") or "direct"),
                "premium_field": premium_field,
                "premium_bps": premium_bps,
                "premium_abs_bps": abs(premium_bps) if premium_bps is not None else None,
                "pnl_bps": pnl_bps,
                "freshness_bucket": freshness_bucket,
                "liquidity_bucket": liquidity_bucket,
                "regional_segmentation": regional_segmentation,
                "strict_stale_filter_pass": strict_stale_pass,
                "synthetic_short_cost_penalty_bps": synthetic_penalty_bps,
                "basis_cost_bucket": _basis_cost_bucket(candidate, synthetic_penalty_bps),
                "base_edge_bps": base_edge_bps,
                "recomputed_edge_bps": recomputed_edge_bps,
                "premium_regime": _premium_regime(direction, premium_bps, pnl_bps),
            }
        )

    _assign_premium_deciles(diagnostic_rows)
    frontier_rows = [row for row in diagnostic_rows if row["cohort"] == "frontier_short"]
    control_rows = [row for row in diagnostic_rows if row["cohort"] == "nearby_control"]
    frontier_valid = [row for row in frontier_rows if row.get("pnl_bps") is not None]
    control_valid = [row for row in control_rows if row.get("pnl_bps") is not None]
    overall_control_avg_pnl = _safe_avg([float(row["pnl_bps"]) for row in control_valid])
    frontier_regimes = Counter(str(row.get("premium_regime") or "neutral_or_unpriced") for row in frontier_rows)
    control_regimes = Counter(str(row.get("premium_regime") or "neutral_or_unpriced") for row in control_rows)

    worst_subsets: list[dict] = []
    for dimension in ("freshness_bucket", "liquidity_bucket", "regional_segmentation", "basis_cost_bucket"):
        for bucket in _subset_summary(diagnostic_rows, dimension):
            if bucket["frontier_short_valid_outcome_count"] <= 0:
                continue
            underperformance = bucket.get("underperformance_vs_control_bps")
            if underperformance is None and bucket["frontier_short_avg_pnl_bps"] is None:
                continue
            worst_subsets.append(
                {
                    "dimension": dimension,
                    "bucket": bucket["bucket"],
                    "frontier_short_valid_outcome_count": bucket["frontier_short_valid_outcome_count"],
                    "frontier_short_avg_pnl_bps": bucket["frontier_short_avg_pnl_bps"],
                    "control_avg_pnl_bps": bucket["control_avg_pnl_bps"],
                    "underperformance_vs_control_bps": underperformance,
                }
            )
    worst_subsets.sort(
        key=lambda item: (
            item["underperformance_vs_control_bps"] is None,
            item["underperformance_vs_control_bps"] if item["underperformance_vs_control_bps"] is not None else item["frontier_short_avg_pnl_bps"] or 0.0,
        )
    )
    def _subset_underperforming(dimension: str, bucket: str) -> bool:
        for item in worst_subsets:
            if item["dimension"] != dimension or item["bucket"] != bucket:
                continue
            delta = item.get("underperformance_vs_control_bps")
            if delta is not None:
                return delta < 0.0
            frontier_avg = item.get("frontier_short_avg_pnl_bps")
            return (
                frontier_avg is not None
                and overall_control_avg_pnl is not None
                and frontier_avg < overall_control_avg_pnl
            )
        return False
    concentration_flags = {
        "stale_subset_underperforming": _subset_underperforming("freshness_bucket", "stale"),
        "illiquid_subset_underperforming": _subset_underperforming("liquidity_bucket", "thin"),
        "high_basis_subset_underperforming": _subset_underperforming("basis_cost_bucket", "high_basis"),
    }

    return {
        "paper_only": True,
        "read_only": True,
        "horizon_minutes": horizon,
        "evidence": list(_FRONTIER_SHORT_DIAGNOSTIC_EVIDENCE),
        "frontier_short_count": len(frontier_rows),
        "frontier_short_valid_outcome_count": len(frontier_valid),
        "control_count": len(control_rows),
        "control_valid_outcome_count": len(control_valid),
        "cohort_comparison": {
            "frontier_short_avg_pnl_bps": _round_or_none(_safe_avg([float(row["pnl_bps"]) for row in frontier_valid])),
            "control_avg_pnl_bps": _round_or_none(_safe_avg([float(row["pnl_bps"]) for row in control_valid])),
            "frontier_short_avg_recomputed_edge_bps": _round_or_none(
                _safe_avg([float(row["recomputed_edge_bps"]) for row in frontier_rows if row.get("recomputed_edge_bps") is not None])
            ),
            "control_avg_recomputed_edge_bps": _round_or_none(
                _safe_avg([float(row["recomputed_edge_bps"]) for row in control_rows if row.get("recomputed_edge_bps") is not None])
            ),
        },
        "stricter_stale_print_filter": {
            "frontier_short_excluded_count": sum(1 for row in frontier_rows if not row["strict_stale_filter_pass"]),
            "control_excluded_count": sum(1 for row in control_rows if not row["strict_stale_filter_pass"]),
            "frontier_short_remaining_avg_recomputed_edge_bps": _round_or_none(
                _safe_avg(
                    [
                        float(row["recomputed_edge_bps"])
                        for row in frontier_rows
                        if row.get("recomputed_edge_bps") is not None
                    ]
                )
            ),
            "synthetic_short_extra_cost_bps_avg": _round_or_none(
                _safe_avg([float(row["synthetic_short_cost_penalty_bps"]) for row in frontier_rows])
            ),
        },
        "subset_splits": {
            "reference_freshness": _subset_summary(diagnostic_rows, "freshness_bucket"),
            "venue_liquidity": _subset_summary(diagnostic_rows, "liquidity_bucket"),
            "regional_segmentation": _subset_summary(diagnostic_rows, "regional_segmentation"),
            "basis_cost": _subset_summary(diagnostic_rows, "basis_cost_bucket"),
        },
        "premium_decile_outcomes": _premium_decile_summary(diagnostic_rows),
        "premium_regime_balance": {
            "frontier_short": dict(frontier_regimes),
            "nearby_control": dict(control_regimes),
        },
        "dominant_underperformance_subsets": worst_subsets[:8],
        "market_structure_misclassification_likely": all(concentration_flags.values()),
        "concentration_flags": concentration_flags,
        "candidate_emission": "retained_for_paper_exploration",
        "hard_blocking": False,
        "entry_blocked": False,
    }


def build_paper_exploration_report(
    conn: sqlite3.Connection,
    settings: dict,
    reviewed: list[dict] | None = None,
) -> dict:
    cfg = settings.get("paper_exploration", {})
    decay_quarantine = okx_basis_decay_quarantine_runtime_report(conn, settings)
    cycle_quarantined_count = 0
    cycle_would_have_filled_count = 0
    for item in reviewed or []:
        candidate = (item or {}).get("candidate") or {}
        review = (item or {}).get("review") or {}
        record = okx_basis_decay_candidate_record(candidate)
        if (
            not isinstance(record, dict)
            or not record.get("active")
            or not okx_basis_decay_matches_reason(record.get("reason"))
            or okx_basis_decay_target_signal(candidate) is None
        ):
            continue
        cycle_quarantined_count += 1
        if (
            record.get("paper_fill_allowed")
            and str(review.get("decision") or "").strip()
            in {"approve_paper_trade", "approve_conditional_paper_trade"}
        ):
            cycle_would_have_filled_count += 1
    decay_quarantine = {
        **decay_quarantine,
        "current_cycle_quarantined_count": cycle_quarantined_count,
        "current_cycle_would_have_filled_count": cycle_would_have_filled_count,
    }
    horizon = int(cfg.get("guard_value_horizon_minutes", 60))
    rows = conn.execute(
        """
        select p.id, p.status, p.pnl_bps, p.candidate_json, p.review_json, p.context_json,
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

    frontier_short_diagnostics = _frontier_short_diagnostic_report(rows, horizon)
    unresolved_route_shadow = unresolved_route_requirement_shadow_summary(conn)

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
            "okx_basis_decay_quarantine_active": int(decay_quarantine.get("status") == "active"),
            "okx_basis_decay_quarantine_closed_labels": int(decay_quarantine.get("closed_label_count") or 0),
            "okx_basis_decay_quarantine_quarantined_count": int(decay_quarantine.get("quarantined_count") or 0),
            "okx_basis_decay_quarantine_would_have_filled_count": int(decay_quarantine.get("would_have_filled_count") or 0),
            "okx_basis_decay_quarantine_shadow_valid_outcome_count": int(decay_quarantine.get("shadow_valid_outcome_count") or 0),
            "okx_basis_decay_quarantine_shadow_pnl_bps": decay_quarantine.get("shadow_pnl_bps"),
            "okx_basis_decay_quarantine_current_cycle_quarantined_count": int(
                decay_quarantine.get("current_cycle_quarantined_count") or 0
            ),
            "okx_basis_decay_quarantine_current_cycle_would_have_filled_count": int(
                decay_quarantine.get("current_cycle_would_have_filled_count") or 0
            ),
            "frontier_short_diagnostic_count": int(frontier_short_diagnostics.get("frontier_short_count") or 0),
            "frontier_short_diagnostic_valid_outcome_count": int(
                frontier_short_diagnostics.get("frontier_short_valid_outcome_count") or 0
            ),
            "frontier_short_control_valid_outcome_count": int(
                frontier_short_diagnostics.get("control_valid_outcome_count") or 0
            ),
            "frontier_short_diagnostic_market_structure_misclassification_likely": int(
                bool(frontier_short_diagnostics.get("market_structure_misclassification_likely"))
            ),
            "unresolved_route_requirement_shadow_open_count": int(
                unresolved_route_shadow.get("open_count", 0)
            ),
            "unresolved_route_requirement_shadow_closed_count": int(
                unresolved_route_shadow.get("closed_count", 0)
            ),
            "unresolved_route_requirement_shadow_avg_pnl_bps": unresolved_route_shadow.get("avg_pnl_bps"),
        },
        "decisions_24h": dict(decisions),
        "true_rejection_reasons_24h": dict(true_rejections.most_common()),
        "guard_value": guard_value,
        "current_cycle_lineages": {
            "priceable": dict(current_priceable),
            "admitted_or_deferred": dict(current_admitted),
        },
        "admission_zero_streaks": zero_streaks,
        "okx_basis_decay_quarantine": decay_quarantine,
        "frontier_short_diagnostics": frontier_short_diagnostics,
        "unresolved_route_requirement_shadow": unresolved_route_shadow,
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
        f"- Unresolved-route shadow fills: `{summary['unresolved_route_requirement_shadow_closed_count']}` "
        f"closed / `{summary['unresolved_route_requirement_shadow_open_count']}` open",
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
    unresolved_shadow = report.get("unresolved_route_requirement_shadow") or {}
    lines.extend(
        [
            "",
            "## Unresolved Route Shadow Fills",
            "",
            f"- Exclusion reason: `{unresolved_shadow.get('exclusion_reason')}`",
            f"- Closed shadow fills: `{unresolved_shadow.get('closed_count', 0)}`",
            f"- Open shadow fills: `{unresolved_shadow.get('open_count', 0)}`",
            f"- Avg shadow PnL: `{unresolved_shadow.get('avg_pnl_bps')}` bps",
            "",
            "| Blocker | Open | Closed | Avg bps | Win rate | Total bps |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for blocker, blocker_summary in sorted((unresolved_shadow.get("by_blocker") or {}).items()):
        lines.append(
            f"| {blocker} | {blocker_summary.get('open_count', 0)} | "
            f"{blocker_summary.get('closed_count', 0)} | {blocker_summary.get('avg_pnl_bps')} | "
            f"{blocker_summary.get('win_rate')} | {blocker_summary.get('total_pnl_bps')} |"
        )
    decay_quarantine = report.get("okx_basis_decay_quarantine") or {}
    lines.extend(
        [
            "",
            "## OKX Basis Decay Quarantine",
            "",
            f"- Reason: `{decay_quarantine.get('reason', OKX_BASIS_DECAY_QUARANTINE_REASON)}`",
            f"- Status: `{decay_quarantine.get('status', 'not_started')}`",
            f"- Quarantined count: `{decay_quarantine.get('quarantined_count', 0)}`",
            f"- Would-have-filled count: `{decay_quarantine.get('would_have_filled_count', 0)}`",
            f"- Current-cycle quarantined / would-have-filled: `{decay_quarantine.get('current_cycle_quarantined_count', 0)}` / `{decay_quarantine.get('current_cycle_would_have_filled_count', 0)}`",
            f"- Subsequent shadow PnL: `{decay_quarantine.get('shadow_pnl_bps')}` bps across `{decay_quarantine.get('shadow_valid_outcome_count', 0)}` valid outcomes",
            f"- Closed labels: `{decay_quarantine.get('closed_label_count', 0)}` / `{decay_quarantine.get('closed_label_limit', 100)}`",
            f"- Labels remaining: `{decay_quarantine.get('closed_label_remaining', 100)}`",
            f"- Label avg PnL / win rate: `{decay_quarantine.get('avg_pnl_bps')}` bps / `{decay_quarantine.get('win_rate')}`",
            f"- Expires: `{decay_quarantine.get('expires_at')}`",
        ]
    )
    frontier_short = report.get("frontier_short_diagnostics") or {}
    cohort_comparison = frontier_short.get("cohort_comparison") or {}
    lines.extend(
        [
            "",
            "## Frontier Short Diagnostics",
            "",
            f"- Frontier short rows / valid outcomes: `{frontier_short.get('frontier_short_count', 0)}` / `{frontier_short.get('frontier_short_valid_outcome_count', 0)}`",
            f"- Nearby control rows / valid outcomes: `{frontier_short.get('control_count', 0)}` / `{frontier_short.get('control_valid_outcome_count', 0)}`",
            f"- Frontier short avg pnl vs control avg pnl: `{cohort_comparison.get('frontier_short_avg_pnl_bps')}` / `{cohort_comparison.get('control_avg_pnl_bps')}` bps",
            f"- Recomputed edge after strict stale filter: `{cohort_comparison.get('frontier_short_avg_recomputed_edge_bps')}` frontier vs `{cohort_comparison.get('control_avg_recomputed_edge_bps')}` control",
            f"- Market-structure misclassification likely: `{frontier_short.get('market_structure_misclassification_likely', False)}`",
            "",
            "| Split | Bucket | Frontier valid | Frontier avg pnl | Control valid | Control avg pnl | Delta vs control |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for split_name in ("reference_freshness", "venue_liquidity", "regional_segmentation", "basis_cost"):
        for item in (frontier_short.get("subset_splits") or {}).get(split_name, [])[:3]:
            lines.append(
                f"| {split_name} | {item['bucket']} | {item['frontier_short_valid_outcome_count']} | "
                f"{item['frontier_short_avg_pnl_bps']} | {item['control_valid_outcome_count']} | "
                f"{item['control_avg_pnl_bps']} | {item['underperformance_vs_control_bps']} |"
            )
    lines.extend(
        [
            "",
            "### Premium Deciles",
            "",
            "| Decile | Frontier valid | Frontier avg pnl | Control valid | Control avg pnl | Mean reversion | Continuation |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in (frontier_short.get("premium_decile_outcomes") or [])[:10]:
        lines.append(
            f"| {item['premium_abs_decile']} | {item['frontier_short_valid_outcome_count']} | "
            f"{item['frontier_short_avg_pnl_bps']} | {item['control_valid_outcome_count']} | "
            f"{item['control_avg_pnl_bps']} | {item['frontier_short_mean_reversion_rate']} | "
            f"{item['frontier_short_continuation_rate']} |"
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
        "okx_basis_decay_quarantine": report.get("okx_basis_decay_quarantine") or {},
        "unresolved_route_requirement_shadow": report.get("unresolved_route_requirement_shadow") or {},
        "frontier_short_diagnostics": {
            "evidence": list((report.get("frontier_short_diagnostics") or {}).get("evidence") or []),
            "frontier_short_count": (report.get("frontier_short_diagnostics") or {}).get("frontier_short_count", 0),
            "frontier_short_valid_outcome_count": (
                (report.get("frontier_short_diagnostics") or {}).get("frontier_short_valid_outcome_count", 0)
            ),
            "control_valid_outcome_count": (
                (report.get("frontier_short_diagnostics") or {}).get("control_valid_outcome_count", 0)
            ),
            "cohort_comparison": ((report.get("frontier_short_diagnostics") or {}).get("cohort_comparison") or {}),
            "subset_splits": {
                key: value[:5]
                for key, value in (((report.get("frontier_short_diagnostics") or {}).get("subset_splits") or {})).items()
            },
            "premium_decile_outcomes": list(
                ((report.get("frontier_short_diagnostics") or {}).get("premium_decile_outcomes") or [])
            )[:5],
            "dominant_underperformance_subsets": list(
                ((report.get("frontier_short_diagnostics") or {}).get("dominant_underperformance_subsets") or [])
            )[:5],
            "market_structure_misclassification_likely": bool(
                (report.get("frontier_short_diagnostics") or {}).get("market_structure_misclassification_likely")
            ),
            "concentration_flags": ((report.get("frontier_short_diagnostics") or {}).get("concentration_flags") or {}),
        },
    }
