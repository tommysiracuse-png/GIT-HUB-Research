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


def _decimal_places(value: object) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "e" in text.lower():
        number = _coerce_float(value)
        if number is None:
            return None
        text = f"{number:.12f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))


def _increment_from_precision(precision: object) -> float | None:
    try:
        digits = int(precision)
    except (TypeError, ValueError):
        return None
    if digits < 0:
        return None
    return round(10.0 ** (-digits), digits) if digits > 0 else 1.0


def _merge_venue_constraints(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("instrument_metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    existing = metadata.get("venue_constraints")
    existing = dict(existing) if isinstance(existing, Mapping) else {}
    top_level = source.get("venue_constraints")
    top_level = dict(top_level) if isinstance(top_level, Mapping) else {}
    merged = {**existing, **top_level}

    price_precision = merged.get("price_precision")
    if price_precision is None:
        price_samples = [
            source.get("best_bid"),
            source.get("best_ask"),
            source.get("bid"),
            source.get("ask"),
            source.get("last"),
            source.get("last_price"),
            source.get("mid_price"),
        ]
        precisions = [value for value in (_decimal_places(sample) for sample in price_samples) if value is not None]
        if precisions:
            price_precision = max(precisions)

    quantity_precision = merged.get("quantity_precision")
    if quantity_precision is None:
        quantity_samples = [source.get("bid_size"), source.get("ask_size")]
        shallow = source.get("shallow_order_book")
        if isinstance(shallow, Mapping):
            for side in ("bids", "asks"):
                levels = shallow.get(side)
                if isinstance(levels, Sequence):
                    for level in levels[:2]:
                        if isinstance(level, Sequence) and len(level) >= 2:
                            quantity_samples.append(level[1])
        precisions = [value for value in (_decimal_places(sample) for sample in quantity_samples) if value is not None]
        if precisions:
            quantity_precision = max(precisions)

    normalized = {
        "price_precision": price_precision,
        "price_increment": merged.get("price_increment")
        if merged.get("price_increment") is not None
        else _increment_from_precision(price_precision),
        "quantity_precision": quantity_precision,
        "quantity_increment": merged.get("quantity_increment")
        if merged.get("quantity_increment") is not None
        else _increment_from_precision(quantity_precision),
        "min_order_quantity": _coerce_float(merged.get("min_order_quantity")),
        "min_order_notional_quote": _coerce_float(merged.get("min_order_notional_quote")),
        "constraint_source": merged.get("constraint_source") or "observed_public_payload",
    }
    normalized["complete"] = bool(
        normalized["price_increment"] is not None
        and normalized["quantity_increment"] is not None
        and normalized["min_order_quantity"] is not None
    )
    return normalized


def native_spot_surface_fields(observation: Mapping[str, object]) -> dict[str, object]:
    """Return portable read-only public-spot fields for frontier candidates.

    The helper distinguishes native venue data from synthetic research but does
    not make an admission decision; callers retain price-safety checks.
    """

    source = dict(observation) if isinstance(observation, Mapping) else {}
    venue = str(source.get("venue") or "").upper() or None
    symbol = str(source.get("symbol") or "").upper() or None
    base = str(source.get("base") or source.get("base_asset") or "").upper() or None
    quote = str(source.get("quote") or source.get("quote_asset") or "").upper() or None
    venue_symbol = str(source.get("venue_symbol") or symbol or "").upper() or None
    metadata = source.get("instrument_metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    venue_constraints = _merge_venue_constraints(source)
    metadata.update(
        {
            "venue": venue,
            "venue_symbol": venue_symbol,
            "base_asset": base,
            "quote_asset": quote,
            "market_type": "spot",
            "public_read_only": True,
            "venue_constraints": venue_constraints,
        }
    )
    shallow_order_book = source.get("shallow_order_book")
    if not isinstance(shallow_order_book, Mapping):
        levels = source.get("book_levels")
        if isinstance(levels, Mapping):
            shallow_order_book = {
                "bids": list(levels.get("bids") or [])[:5],
                "asks": list(levels.get("asks") or [])[:5],
            }
        else:
            shallow_order_book = None
    return {
        "market_data_origin": "native_public_spot",
        "synthetic_research": False,
        "instrument_metadata": metadata,
        "venue_constraints": venue_constraints,
        "best_bid": _coerce_float(source.get("best_bid") if source.get("best_bid") is not None else source.get("bid")),
        "best_ask": _coerce_float(source.get("best_ask") if source.get("best_ask") is not None else source.get("ask")),
        "last_trade_timestamp": source.get("last_trade_timestamp") or source.get("exchange_timestamp"),
        "shallow_order_book": shallow_order_book,
    }


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
