"""Paper-only LLM state packet helpers.

This module composes JSON-serializable fragments from existing paper/research
reports.  It has no side effects: no file writes, credential collection,
private API calls, order routing, or live trading enablement.
"""

from __future__ import annotations

from typing import Any, Iterable

try:  # Support both direct ``src`` imports and package-style imports.
    from route_intelligence import build_route_requirements_report
except ImportError:  # pragma: no cover - package import fallback
    from .route_intelligence import build_route_requirements_report


def build_route_intelligence_packet_fragment(
    opportunities: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return a paper-only route-intelligence fragment for an LLM packet.

    The fragment is read-only and derived entirely from caller-supplied paper
    opportunities.  It intentionally does not collect credentials, call broker
    APIs, mutate order/fill state, or enable any live execution path.
    """

    return {
        "paper_only": True,
        "safety_constraints": [
            "read_only_output_only",
            "no_credentials",
            "no_live_trading",
        ],
        "route_intelligence_report": build_route_requirements_report(opportunities),
    }
