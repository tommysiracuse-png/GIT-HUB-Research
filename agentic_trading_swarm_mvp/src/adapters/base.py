"""Base contracts for market-data adapter plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdapterInfo:
    adapter_id: str
    venue: str
    market_type: str
    source: str


class MarketDataAdapter(Protocol):
    info: AdapterInfo

    def scan(self) -> list[dict]:
        """Return normalized public observations."""
