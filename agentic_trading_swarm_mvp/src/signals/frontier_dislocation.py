"""Wrapper marker for the existing frontier dislocation signal."""

from __future__ import annotations

from .base import SignalInfo


info = SignalInfo(signal_id="frontier_dislocation_existing", family="frontier_crypto_venue_map", version="existing-wrapper")


def generate(observations: list[dict]) -> list[dict]:
    return []
