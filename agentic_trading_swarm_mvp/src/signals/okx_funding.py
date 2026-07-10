"""Wrapper marker for the existing OKX funding signal."""

from __future__ import annotations

from .base import SignalInfo


info = SignalInfo(signal_id="okx_funding_existing", family="OKX|perp_funding_basis", version="existing-wrapper")


def generate(observations: list[dict]) -> list[dict]:
    return []
