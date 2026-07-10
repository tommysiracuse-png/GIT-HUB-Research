"""Registry for signal plugins."""

from __future__ import annotations

from typing import Any


_SIGNALS: dict[str, Any] = {}


def register_signal(signal: Any) -> Any:
    signal_id = getattr(getattr(signal, "info", None), "signal_id", None)
    if not signal_id:
        raise ValueError("signal.info.signal_id is required")
    _SIGNALS[str(signal_id)] = signal
    return signal


def get_signal(signal_id: str) -> Any | None:
    return _SIGNALS.get(signal_id)


def list_signals() -> list[str]:
    return sorted(_SIGNALS)
