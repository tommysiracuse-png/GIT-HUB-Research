"""Registry for market-data adapter plugins."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any


_ADAPTERS: dict[str, Any] = {}


def register_adapter(adapter: Any) -> Any:
    adapter_id = getattr(getattr(adapter, "info", None), "adapter_id", None)
    if not adapter_id:
        raise ValueError("adapter.info.adapter_id is required")
    _ADAPTERS[str(adapter_id)] = adapter
    return adapter


def get_adapter(adapter_id: str) -> Any | None:
    return _ADAPTERS.get(adapter_id)


def list_adapters() -> list[str]:
    return sorted(_ADAPTERS)


def discover_adapters() -> list[str]:
    """Import venue plugin modules and return every registered adapter id."""

    package = importlib.import_module("adapters.venues")
    for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if module.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        importlib.import_module(module.name)
    return list_adapters()


def adapter_records() -> list[dict[str, Any]]:
    discover_adapters()
    records: list[dict[str, Any]] = []
    for adapter_id in list_adapters():
        adapter = _ADAPTERS[adapter_id]
        info = adapter.info
        records.append(
            {
                "adapter_id": str(info.adapter_id),
                "venue": str(info.venue),
                "market_type": str(info.market_type),
                "source": str(info.source),
                "capabilities": sorted(set(info.capabilities)),
                "aliases": sorted(set(info.aliases)),
                "docs_url": info.docs_url,
                "runtime_entrypoint": info.runtime_entrypoint,
                "quote_assets": sorted(set(info.quote_assets)),
                "active": bool(info.active),
                "implementation": "plugin",
            }
        )
    return records
