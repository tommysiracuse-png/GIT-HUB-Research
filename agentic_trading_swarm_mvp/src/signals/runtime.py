"""Runtime discovery and execution for promoted paper-only signal plugins."""

from __future__ import annotations

import collections
import sqlite3

from strategy_program import build_feature_frames

from .registry import discover_signals, generate_registered_signal_candidates


def _bounded_runtime_observations(observations: list[dict], limit: int) -> list[dict]:
    """Keep feature-history fan-out bounded while retaining venue breadth."""

    if len(observations) <= limit:
        return observations
    by_venue: dict[str, collections.deque[dict]] = {}
    for observation in observations:
        venue = str(observation.get("venue") or "UNKNOWN")
        by_venue.setdefault(venue, collections.deque()).append(observation)
    selected: list[dict] = []
    venues = sorted(by_venue)
    while len(selected) < limit:
        progressed = False
        for venue in venues:
            if by_venue[venue]:
                selected.append(by_venue[venue].popleft())
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


def run_signal_plugins(
    conn: sqlite3.Connection,
    observations: dict[str, dict] | list[dict],
    settings: dict,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("enabled", True):
        return [], {"enabled": False, "reason": "strategy_lab_disabled"}
    if not cfg.get("promoted_signal_plugins_enabled", True):
        return [], {"enabled": False, "reason": "promoted_signal_plugins_disabled"}
    discovery = discover_signals()
    raw_observations = list(observations.values()) if isinstance(observations, dict) else list(observations or [])
    runtime_limit = max(1, int(cfg.get("promoted_signal_runtime_max_observations", 500)))
    runtime_observations = _bounded_runtime_observations(raw_observations, runtime_limit)
    feature_frames = build_feature_frames(conn, runtime_observations, settings)
    candidates, generation = generate_registered_signal_candidates(
        runtime_observations,
        context={
            "conn": conn,
            "settings": settings,
            "feature_frames": feature_frames,
        },
    )
    # Import lazily because strategy_lab discovers this package while loading.
    from strategy_lab import enforce_promoted_strategy_lab_surface_policy

    candidates, surface_policy = enforce_promoted_strategy_lab_surface_policy(
        conn,
        candidates,
    )
    return candidates, {
        "enabled": True,
        "discovery": discovery,
        "raw_observation_count": len(raw_observations),
        "runtime_observation_count": len(runtime_observations),
        "omitted_observation_count": len(raw_observations) - len(runtime_observations),
        "runtime_observation_limit": runtime_limit,
        "feature_frame_count": len(feature_frames),
        "activated_candidate_count": len(candidates),
        "surface_policy": surface_policy,
        **generation,
    }
