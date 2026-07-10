"""Base contracts for signal plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SignalInfo:
    signal_id: str
    family: str
    version: str


class SignalPlugin(Protocol):
    info: SignalInfo

    def generate(self, observations: list[dict]) -> list[dict]:
        """Return normalized paper-only candidates."""
