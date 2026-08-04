"""Runtime discovery and execution for promoted paper-only signal plugins."""

from __future__ import annotations

import sqlite3

from strategy_program import build_feature_frames

from .registry import discover_signals, generate_registered_signal_candidates


def run_signal_plugins(
    conn: sqlite3.Connection,
    observations: dict[str, dict] | list[dict],
    settings: dict,
) -> tuple[list[dict], dict]:
    cfg = settings.get("strategy_lab", {})
    if not cfg.get("promoted_signal_plugins_enabled", True):
        return [], {"enabled": False}
    discovery = discover_signals()
    raw_observations = list(observations.values()) if isinstance(observations, dict) else list(observations or [])
    feature_frames = build_feature_frames(conn, observations, settings)
    candidates, generation = generate_registered_signal_candidates(
        raw_observations,
        context={
            "conn": conn,
            "settings": settings,
            "feature_frames": feature_frames,
        },
    )
    return candidates, {
        "enabled": True,
        "discovery": discovery,
        "feature_frame_count": len(feature_frames),
        **generation,
    }
