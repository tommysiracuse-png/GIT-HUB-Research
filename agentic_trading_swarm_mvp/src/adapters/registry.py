"""Registry for market-data adapter plugins."""

from __future__ import annotations

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
