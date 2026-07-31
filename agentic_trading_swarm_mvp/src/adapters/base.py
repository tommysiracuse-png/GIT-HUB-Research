"""Base contracts for market-data adapter plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scan_batch import ScanBatch


@dataclass(frozen=True)
class AdapterInfo:
    adapter_id: str
    venue: str
    market_type: str
    source: str
    capabilities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    docs_url: str | None = None
    runtime_entrypoint: str | None = None
    quote_assets: tuple[str, ...] = ()
    default_cache_minutes: int = 60
    active: bool = True


class MarketDataAdapter(Protocol):
    info: AdapterInfo

    def scan(self, settings: dict | None = None) -> ScanBatch | list[dict]:
        """Return a normalized public-data batch or legacy observation list."""
