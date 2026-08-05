"""Evidence-backed, sticky evaluation-horizon selection."""

from __future__ import annotations

from typing import Any


DEFAULT_HORIZONS_MINUTES = (5, 15, 60, 240, 1440)


def candidate_horizons(
    settings: dict,
    section: str,
    custom_rules: dict | None = None,
) -> list[int]:
    """Return valid horizons, honoring an explicitly fixed strategy contract."""
    custom = custom_rules or {}
    section_cfg = settings.get(section, {})
    mode = str(custom.get("horizon_mode") or section_cfg.get("evaluation_horizon_mode") or "best_reliable")
    explicit = custom.get("horizon_minutes")
    if mode == "fixed" and explicit is not None:
        values: Any = [explicit]
    else:
        values = (
            custom.get("candidate_horizons_minutes")
            or section_cfg.get("candidate_horizons_minutes")
            or settings.get("learning", {}).get("horizon_minutes")
            or DEFAULT_HORIZONS_MINUTES
        )
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    horizons = sorted({int(value) for value in values if int(value) > 0})
    return horizons or list(DEFAULT_HORIZONS_MINUTES)


def prior_selected_horizon(evaluation: dict | None) -> int | None:
    if not isinstance(evaluation, dict):
        return None
    value = evaluation.get("selected_horizon_minutes")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def select_sticky_horizon(
    evaluations: dict[int, dict],
    previous_horizon: int | None = None,
    switch_uplift_bps: float = 6.0,
) -> dict:
    """Select the strongest evidence tier without chasing small score changes."""
    if not evaluations:
        return {
            "selected_horizon_minutes": None,
            "previous_horizon_minutes": previous_horizon,
            "horizon_changed": False,
            "selection_reason": "no_horizon_evidence",
            "selected_evaluation": {},
        }

    def tier(item: dict) -> int:
        if item.get("passed"):
            return 2
        if item.get("ready"):
            return 1
        return 0

    def score(item: dict) -> float:
        value = item.get("selection_score_bps")
        try:
            return float(value) if value is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    def evidence(item: dict) -> int:
        try:
            return int(item.get("evidence_count") or 0)
        except (TypeError, ValueError):
            return 0

    best_horizon, best = max(
        evaluations.items(),
        key=lambda pair: (tier(pair[1]), score(pair[1]), evidence(pair[1]), -int(pair[0])),
    )
    selected_horizon = int(best_horizon)
    reason = "best_available_evidence"

    previous = evaluations.get(int(previous_horizon)) if previous_horizon is not None else None
    if previous is not None and int(previous_horizon) != selected_horizon:
        same_tier = tier(previous) == tier(best)
        previous_score = score(previous)
        best_score = score(best)
        if same_tier and best_score < previous_score + float(switch_uplift_bps):
            selected_horizon = int(previous_horizon)
            reason = "sticky_horizon_retained"
        elif tier(best) > tier(previous):
            reason = "stronger_evidence_tier"
        else:
            reason = "material_horizon_uplift"
    elif previous_horizon is not None:
        reason = "existing_horizon_remains_best"

    return {
        "selected_horizon_minutes": selected_horizon,
        "previous_horizon_minutes": previous_horizon,
        "horizon_changed": previous_horizon is not None and int(previous_horizon) != selected_horizon,
        "selection_reason": reason,
        "selected_evaluation": evaluations[selected_horizon],
    }
