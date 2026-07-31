"""Paper-only frontier venue mapping helpers.

This module provides a safe leave-one-out reference utility for frontier
signal research. It does not place orders or talk to brokers.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class LeaveOneOutFrontierSignal:
    venue: str
    reference_median: float
    venue_value: float
    delta_vs_reference: float
    eligible_peer_count: int

    @property
    def is_favorable(self) -> bool:
        return self.delta_vs_reference > 0


def _coerce_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_leave_one_out_frontier_signals(
    venue_values: Mapping[str, object],
    *,
    min_peer_count: int = 2,
) -> list[LeaveOneOutFrontierSignal]:
    """Build paper-only leave-one-out frontier signals.

    Each venue is compared against the median of all other eligible venues.
    A result is emitted only when the venue has at least ``min_peer_count``
    peers available after leave-one-out filtering.
    """

    cleaned: dict[str, float] = {}
    for venue, raw_value in venue_values.items():
        value = _coerce_float(raw_value)
        if venue and value is not None:
            cleaned[str(venue)] = value

    if len(cleaned) < max(1, min_peer_count + 1):
        return []

    signals: list[LeaveOneOutFrontierSignal] = []
    venues = list(cleaned.keys())
    for venue in venues:
        peer_values = [cleaned[v] for v in venues if v != venue]
        if len(peer_values) < min_peer_count:
            continue
        reference = float(median(peer_values))
        venue_value = cleaned[venue]
        signals.append(
            LeaveOneOutFrontierSignal(
                venue=venue,
                reference_median=reference,
                venue_value=venue_value,
                delta_vs_reference=venue_value - reference,
                eligible_peer_count=len(peer_values),
            )
        )
    return signals


def choose_favorable_frontier_signal(
    venue_values: Mapping[str, object],
    *,
    min_peer_count: int = 2,
) -> LeaveOneOutFrontierSignal | None:
    """Return the strongest favorable paper-only frontier signal, if any."""

    signals = build_leave_one_out_frontier_signals(
        venue_values,
        min_peer_count=min_peer_count,
    )
    favorable = [signal for signal in signals if signal.is_favorable]
    if not favorable:
        return None
    favorable.sort(key=lambda signal: (signal.delta_vs_reference, signal.eligible_peer_count), reverse=True)
    return favorable[0]
