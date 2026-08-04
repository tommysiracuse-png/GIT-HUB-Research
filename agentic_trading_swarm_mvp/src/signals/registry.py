"""Registry for signal plugins."""

from __future__ import annotations

import importlib
import pkgutil
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


def discover_signals(package_name: str = "signals.generated") -> dict:
    """Import generated signal modules and register their module contracts."""

    discovered: list[str] = []
    errors: list[dict] = []
    importlib.invalidate_caches()
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError as exc:
        return {"package": package_name, "discovered": [], "errors": [{"module": package_name, "error": str(exc)}]}
    for info in pkgutil.iter_modules(getattr(package, "__path__", []), package.__name__ + "."):
        if info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        try:
            module = importlib.import_module(info.name)
            register_signal(module)
            discovered.append(str(module.info.signal_id))
        except Exception as exc:  # A broken generated plugin must not stop the radar.
            errors.append({"module": info.name, "error": f"{type(exc).__name__}: {exc}"})
    return {"package": package_name, "discovered": sorted(discovered), "errors": errors}


def known_strategy_signatures() -> dict[str, str]:
    output = {}
    for signal_id, signal in _SIGNALS.items():
        signature = getattr(getattr(signal, "info", None), "strategy_signature", None)
        if signature:
            output[str(signature)] = signal_id
    return output


def promoted_strategy_lab_ids() -> dict[str, str]:
    output = {}
    for signal_id, signal in _SIGNALS.items():
        strategy_lab_id = getattr(getattr(signal, "info", None), "strategy_lab_id", None)
        if strategy_lab_id:
            output[str(strategy_lab_id)] = signal_id
    return output


def generate_registered_signal_candidates(
    observations: list[dict],
    *,
    context: dict | None = None,
) -> tuple[list[dict], dict]:
    """Run registered signals without allowing one plugin to break the loop."""

    generated: list[dict] = []
    by_signal: dict[str, int] = {}
    errors: list[dict] = []
    for signal_id in list_signals():
        signal = _SIGNALS[signal_id]
        try:
            try:
                rows = signal.generate(observations, context=context)
            except TypeError:
                rows = signal.generate(observations)
            accepted = 0
            for raw in rows or []:
                if not isinstance(raw, dict):
                    continue
                required = {"venue", "inst_id", "direction", "trade_type", "last", "score"}
                if required - set(raw):
                    continue
                candidate = dict(raw)
                info = signal.info
                candidate.setdefault("signal_plugin_id", signal_id)
                candidate.setdefault("signal_plugin_family", str(info.family))
                candidate.setdefault("signal_plugin_version", str(info.version))
                if getattr(info, "strategy_lab_id", None):
                    candidate.setdefault("strategy_lab_id", str(info.strategy_lab_id))
                candidate.setdefault("strategy_lab_logic_type", "generated_signal_plugin")
                generated.append(candidate)
                accepted += 1
            by_signal[signal_id] = accepted
        except Exception as exc:
            errors.append({"signal_id": signal_id, "error": f"{type(exc).__name__}: {exc}"})
    return generated, {
        "registered_signal_count": len(_SIGNALS),
        "generated_candidate_count": len(generated),
        "generated_by_signal": by_signal,
        "errors": errors,
    }
