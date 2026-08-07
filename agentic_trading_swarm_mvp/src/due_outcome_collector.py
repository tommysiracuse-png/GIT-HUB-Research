"""Bounded public-candle collection for due paper outcome labels.

This module deliberately has no database or model dependencies.  A caller supplies
the due items and may later persist the returned records in the transaction that
owns those items.  Venue and instrument identity always comes from the trusted
request configuration; response identity, when present, is only used as a veto.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol


UTC = dt.timezone.utc
SOURCE_KIND = "exchange_candle_1m_close"
FUNDING_SOURCE_KIND = "exchange_realized_funding_event"
QUALITY_STATUS = "verified"
INTERVAL_SECONDS = 60
HARD_MAX_INSTRUMENTS = 100
HARD_MAX_WORKERS = 4
HARD_MAX_REQUEST_TIMEOUT_SECONDS = 8.0
HARD_MAX_COLLECTION_TIMEOUT_SECONDS = 60.0
HARD_MAX_RESPONSE_BYTES = 2_000_000
HARD_MAX_CANDLES_PER_REQUEST = 100
SUPPORTED_PARSERS = {
    "okx_1m_candles",
    "gate_1m_candles",
    "binance_style_1m_klines",
    "bybit_v5_1m_klines",
}


def _utc(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        numeric = float(value)
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000.0
        try:
            parsed = dt.datetime.fromtimestamp(numeric, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def canonical_venue(value: object) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def canonical_market_surface(value: object) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"spot", "crypto_spot", "cash"}:
        return "spot"
    if token in {
        "perp",
        "linear",
        "swap",
        "perpetual",
        "perpetual_swap",
        "crypto_derivatives",
        "okx_perpetual_swap",
    }:
        return "perpetual_swap"
    return token


def canonical_symbol(value: object) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def _symbol_token(value: object) -> str:
    return "".join(character for character in canonical_symbol(value) if character.isalnum())


def canonical_instrument_id(venue: object, instrument_id: object, symbol: object) -> str:
    canonical_venue_value = canonical_venue(venue)
    raw = str(instrument_id or "").strip()
    raw_symbol = raw.split(":", 1)[1] if ":" in raw else raw
    canonical_symbol_value = canonical_symbol(symbol or raw_symbol)
    if not canonical_venue_value or not canonical_symbol_value:
        return ""
    # Preserve the storage/entry-contract identity exactly (apart from casing).
    # OKX perp ids are unprefixed while frontier ids commonly include a venue
    # prefix; manufacturing a new prefix would make a valid candle unjoinable.
    return canonical_symbol(raw) if raw else canonical_symbol_value


@dataclasses.dataclass(frozen=True)
class DueInstrument:
    """One not-yet-recorded outcome whose target time has arrived."""

    outcome_key: str
    venue: str
    instrument_id: str
    symbol: str
    market_surface: str
    target_at: dt.datetime
    horizon_minutes: int
    requires_funding_events: bool = False
    funding_window_start_at: dt.datetime | None = None
    due_window_key: str | None = None
    due_window_start_at: dt.datetime | None = None
    due_window_end_at: dt.datetime | None = None
    due_window_max_candles: int | None = None


@dataclasses.dataclass(frozen=True)
class CandleSource:
    """Trusted, credential-free public endpoint definition."""

    venue: str
    market_surface: str
    source_name: str
    url_template: str
    parser: str
    rate_limit_per_second: float
    max_candles: int = 100
    rate_limit_key: str | None = None
    category: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return canonical_venue(self.venue), canonical_market_surface(self.market_surface)


@dataclasses.dataclass(frozen=True)
class FundingSource:
    venue: str = "OKX"
    market_surface: str = "perpetual_swap"
    source_name: str = "OKX public REST realized funding history"
    url_template: str = (
        "https://www.okx.com/api/v5/public/funding-rate-history?"
        "instId={symbol}&limit=400"
    )
    parser: str = "okx_realized_funding_history"
    endpoint: str = "/api/v5/public/funding-rate-history"
    rate_limit_per_second: float = 8.0
    rate_limit_key: str = "OKX"
    max_events: int = 400


@dataclasses.dataclass(frozen=True)
class CandleRequest:
    venue: str
    instrument_id: str
    symbol: str
    market_surface: str
    source_name: str
    parser: str
    url: str
    window_start_at: dt.datetime
    window_end_at: dt.datetime
    source: CandleSource


@dataclasses.dataclass(frozen=True)
class FundingRequest:
    venue: str
    instrument_id: str
    symbol: str
    market_surface: str
    source_name: str
    parser: str
    url: str
    window_start_at: dt.datetime
    window_end_at: dt.datetime
    outcome_keys: tuple[str, ...]
    source: FundingSource


@dataclasses.dataclass(frozen=True)
class CandleFetch:
    ok: bool
    payload: object
    received_at: dt.datetime | str
    http_status: str | None = None
    error: str | None = None
    response_venue: str | None = None
    response_instrument_id: str | None = None
    response_source_name: str | None = None


@dataclasses.dataclass(frozen=True)
class ClosedCandle:
    candle_open_at: dt.datetime
    event_at: dt.datetime
    price: float


@dataclasses.dataclass(frozen=True)
class RealizedFundingEvent:
    event_at: dt.datetime
    realized_rate: float
    method: str | None
    formula_type: str | None


@dataclasses.dataclass(frozen=True)
class ParsedCandleBatch:
    candles: tuple[ClosedCandle, ...]
    partial_event_ats: tuple[dt.datetime, ...]
    invalid_row_count: int
    fatal_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ParsedFundingBatch:
    events: tuple[RealizedFundingEvent, ...]
    raw_row_count: int
    invalid_row_count: int
    fatal_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class OutcomeWindowPlan:
    selected: tuple[DueInstrument, ...]
    deferred: tuple[DueInstrument, ...]
    cursor_outcome_key: str | None
    next_cursor_outcome_key: str | None
    candle_limit: int
    window_start_at: dt.datetime | None
    window_end_at: dt.datetime | None


@dataclasses.dataclass(frozen=True)
class CollectorConfig:
    """Hard-bounded collection policy.

    Five-minute labels are intentionally excluded from this cadence-safe path.
    A caller that needs them must run a separately scheduled due-price collector.
    """

    enabled: bool = True
    candle_interval_seconds: int = INTERVAL_SECONDS
    allowed_horizon_minutes: tuple[int, ...] = (15, 60, 240, 1440)
    max_instruments: int = HARD_MAX_INSTRUMENTS
    max_workers: int = HARD_MAX_WORKERS
    request_timeout_seconds: float = HARD_MAX_REQUEST_TIMEOUT_SECONDS
    collection_timeout_seconds: float = 30.0
    max_delay_seconds: float = 300.0
    okx_max_requests_per_second: float = 8.0
    allow_latest_ticker_fallback: bool = False

    def __post_init__(self) -> None:
        if int(self.candle_interval_seconds) != INTERVAL_SECONDS:
            raise ValueError("paper_due_outcome_collection_requires_closed_1m_candles")
        if self.allow_latest_ticker_fallback:
            raise ValueError("latest_ticker_fallback_is_not_permitted")
        horizons = tuple(int(value) for value in self.allowed_horizon_minutes)
        if 5 in horizons:
            raise ValueError("five_minute_horizon_requires_a_dedicated_due_price_cadence")
        if not horizons or any(value <= 0 for value in horizons):
            raise ValueError("allowed_horizon_minutes_must_be_positive")


class DueInstrumentProvider(Protocol):
    def load_due_instruments(
        self, *, limit: int
    ) -> Iterable[DueInstrument | Mapping[str, object]]: ...


class PublicCandleFetcher(Protocol):
    def fetch(
        self, request: CandleRequest | FundingRequest, *, timeout_seconds: float
    ) -> CandleFetch: ...


def collector_config_from_settings(settings: Mapping[str, object]) -> CollectorConfig:
    """Read the single bounded settings surface used by the campaign runner."""

    raw = settings.get("paper_due_outcome_collection")
    section = raw if isinstance(raw, Mapping) else {}
    return CollectorConfig(
        enabled=bool(section.get("enabled", False)),
        candle_interval_seconds=int(
            section.get("candle_interval_seconds", INTERVAL_SECONDS)
        ),
        max_instruments=min(
            HARD_MAX_INSTRUMENTS,
            max(0, int(section.get("max_instruments_per_cycle", HARD_MAX_INSTRUMENTS))),
        ),
        max_workers=min(
            HARD_MAX_WORKERS,
            max(1, int(section.get("max_workers", HARD_MAX_WORKERS))),
        ),
        request_timeout_seconds=min(
            HARD_MAX_REQUEST_TIMEOUT_SECONDS,
            max(
                0.1,
                float(
                    section.get(
                        "request_timeout_seconds", HARD_MAX_REQUEST_TIMEOUT_SECONDS
                    )
                ),
            ),
        ),
        okx_max_requests_per_second=min(
            8.0,
            max(0.01, float(section.get("okx_max_requests_per_second", 8.0))),
        ),
        allow_latest_ticker_fallback=bool(
            section.get("allow_latest_ticker_fallback", False)
        ),
    )


class UrllibPublicCandleFetcher:
    """Small no-auth HTTP client with a bounded response body."""

    def __init__(self, *, max_response_bytes: int = HARD_MAX_RESPONSE_BYTES) -> None:
        self.max_response_bytes = min(
            HARD_MAX_RESPONSE_BYTES, max(1, int(max_response_bytes))
        )

    def fetch(
        self, request: CandleRequest | FundingRequest, *, timeout_seconds: float
    ) -> CandleFetch:
        received_at = dt.datetime.now(UTC)
        try:
            parsed_url = urllib.parse.urlsplit(request.url)
            if parsed_url.scheme.lower() != "https" or parsed_url.username or parsed_url.password:
                return CandleFetch(
                    ok=False,
                    payload=None,
                    received_at=received_at,
                    error="unsafe_public_source_url",
                )
            http_request = urllib.request.Request(
                request.url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "agentic-trading-swarm/outcome-candle-collector",
                },
            )
            with urllib.request.urlopen(
                http_request, timeout=max(0.1, float(timeout_seconds))
            ) as response:
                body = response.read(self.max_response_bytes + 1)
                received_at = dt.datetime.now(UTC)
                if len(body) > self.max_response_bytes:
                    return CandleFetch(
                        ok=False,
                        payload=None,
                        received_at=received_at,
                        http_status=str(getattr(response, "status", "")),
                        error="response_too_large",
                    )
                return CandleFetch(
                    ok=True,
                    payload=json.loads(body.decode("utf-8")),
                    received_at=received_at,
                    http_status=str(getattr(response, "status", "")),
                )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            return CandleFetch(
                ok=False,
                payload=None,
                received_at=dt.datetime.now(UTC),
                error=f"{type(exc).__name__}:{str(exc)[:180]}",
            )


class VenueRatePacer:
    """Thread-safe start-time pacing shared by every source on an exchange."""

    def __init__(self, *, monotonic=time.monotonic, sleep=time.sleep) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start: dict[str, float] = {}

    def acquire(self, key: str, rate_per_second: float, deadline: float) -> bool:
        rate = max(0.01, float(rate_per_second))
        with self._lock:
            now = self._monotonic()
            scheduled = max(now, self._next_start.get(key, now))
            if scheduled > deadline:
                return False
            self._next_start[key] = scheduled + (1.0 / rate)
        delay = scheduled - self._monotonic()
        if delay > 0:
            self._sleep(delay)
        return self._monotonic() <= deadline


def default_public_candle_sources() -> tuple[CandleSource, ...]:
    """Qualified public 1m sources already represented in the frontier registry."""

    return (
        CandleSource(
            venue="OKX",
            market_surface="perpetual_swap",
            source_name="OKX public REST history candles",
            url_template=(
                "https://www.okx.com/api/v5/market/history-candles?"
                "instId={symbol}&bar=1m&after={end_ms}&limit={limit}"
            ),
            parser="okx_1m_candles",
            rate_limit_per_second=8.0,
            rate_limit_key="OKX",
            max_candles=100,
        ),
        CandleSource(
            venue="OKX_SPOT",
            market_surface="spot",
            source_name="OKX public REST history candles",
            url_template=(
                "https://www.okx.com/api/v5/market/history-candles?"
                "instId={symbol}&bar=1m&after={end_ms}&limit={limit}"
            ),
            parser="okx_1m_candles",
            rate_limit_per_second=8.0,
            rate_limit_key="OKX",
            max_candles=100,
        ),
        CandleSource(
            venue="GATE",
            market_surface="spot",
            source_name="Gate public REST spot candlesticks",
            url_template=(
                "https://api.gateio.ws/api/v4/spot/candlesticks?"
                "currency_pair={symbol}&interval=1m&from={start_seconds}&to={end_seconds}&limit={limit}"
            ),
            parser="gate_1m_candles",
            rate_limit_per_second=5.0,
            max_candles=1000,
        ),
        CandleSource(
            venue="MEXC",
            market_surface="spot",
            source_name="MEXC public REST spot klines",
            url_template=(
                "https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=1m&"
                "startTime={start_ms}&endTime={end_ms}&limit={limit}"
            ),
            parser="binance_style_1m_klines",
            rate_limit_per_second=5.0,
            max_candles=1000,
        ),
        CandleSource(
            venue="BINANCE_US",
            market_surface="spot",
            source_name="Binance US public REST spot klines",
            url_template=(
                "https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1m&"
                "startTime={start_ms}&endTime={end_ms}&limit={limit}"
            ),
            parser="binance_style_1m_klines",
            rate_limit_per_second=5.0,
            max_candles=1000,
        ),
        CandleSource(
            venue="BYBIT_SPOT",
            market_surface="spot",
            source_name="Bybit public REST spot klines",
            url_template=(
                "https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&"
                "interval=1&start={start_ms}&end={end_ms}&limit={limit}"
            ),
            parser="bybit_v5_1m_klines",
            rate_limit_per_second=5.0,
            max_candles=1000,
            category="spot",
        ),
        CandleSource(
            venue="BYBIT",
            market_surface="perpetual_swap",
            source_name="Bybit public REST linear klines",
            url_template=(
                "https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&"
                "interval=1&start={start_ms}&end={end_ms}&limit={limit}"
            ),
            parser="bybit_v5_1m_klines",
            rate_limit_per_second=5.0,
            max_candles=1000,
            category="linear",
        ),
    )


def outcome_measurement_capability(
    venue: object,
    inst_id: object,
    market_surface: object = None,
    trade_type: object = None,
    *,
    symbol: object = None,
    sources: Iterable[CandleSource] | None = None,
) -> dict[str, object]:
    """Describe whether a trade has a qualified closed-candle measurement path.

    This is intentionally pure so admission code can fail closed without copying
    venue tables or importing the collector runtime.  Capability is only for one
    price leg; paired basis/funding PnL still requires explicit matching legs.
    """

    canonical_venue_value = canonical_venue(venue)
    raw_id = str(inst_id or "").strip()
    raw_symbol = symbol or raw_id.split(":", 1)[-1]
    canonical_symbol_value = canonical_symbol(raw_symbol)
    surface = canonical_market_surface(market_surface)
    trade_type_token = str(trade_type or "").strip().lower()
    if surface not in {"spot", "perpetual_swap"}:
        surface = ""
        if (
            canonical_symbol_value.endswith("-SWAP")
            or "perp" in trade_type_token
            or "funding_basis" in trade_type_token
        ):
            surface = "perpetual_swap"
        elif canonical_venue_value.endswith("_SPOT") or trade_type_token == "frontier_crypto_venue_map":
            surface = "spot"
    canonical_id = canonical_instrument_id(
        canonical_venue_value, raw_id, canonical_symbol_value
    )
    base = {
        "capable": False,
        "venue": canonical_venue_value,
        "inst_id": canonical_id,
        "market_surface": surface,
        "source_kind": SOURCE_KIND,
        "candle_interval_seconds": INTERVAL_SECONDS,
        "measurement_scope": "single_leg_price_observation",
        "paired_outcome_complete": False,
        "requires_credentials": False,
        "allow_latest_ticker_fallback": False,
    }
    if not canonical_venue_value or not canonical_symbol_value or not canonical_id or not surface:
        return {**base, "reason": "invalid_or_incomplete_instrument_identity"}
    if ":" in raw_id and canonical_venue(raw_id.split(":", 1)[0]) != canonical_venue_value:
        return {**base, "reason": "instrument_venue_mismatch"}
    if raw_id and _symbol_token(raw_id.split(":", 1)[-1]) != _symbol_token(canonical_symbol_value):
        return {**base, "reason": "instrument_symbol_mismatch"}
    source_lookup = {
        source.key: source for source in (sources or default_public_candle_sources())
    }
    source = source_lookup.get((canonical_venue_value, surface))
    if source is None:
        return {**base, "reason": "unqualified_candle_source"}
    parsed_url = urllib.parse.urlsplit(source.url_template)
    if (
        source.parser not in SUPPORTED_PARSERS
        or parsed_url.scheme.lower() != "https"
        or parsed_url.username
        or parsed_url.password
    ):
        return {**base, "reason": "unsafe_or_unsupported_candle_source"}
    return {
        **base,
        "capable": True,
        "reason": "qualified_closed_1m_public_candles",
        "source_name": source.source_name,
        "parser": source.parser,
        "rate_limit_key": canonical_venue(source.rate_limit_key or source.venue),
    }


def plan_due_instrument_window(
    dues: Iterable[DueInstrument],
    source: CandleSource,
    *,
    max_delay_seconds: float = 300.0,
    cursor_outcome_key: str | None = None,
) -> OutcomeWindowPlan:
    """Plan one fair, bounded historical window for a unique instrument.

    The cursor is an external persistence contract: after each attempt the
    caller stores ``next_cursor_outcome_key``.  If an old missing label remains
    due, the next run starts after it rather than starving newer horizons.
    """

    ordered = sorted(
        dues,
        key=lambda item: (item.target_at, item.outcome_key),
    )
    candle_limit = min(
        HARD_MAX_CANDLES_PER_REQUEST,
        max(1, int(source.max_candles)),
    )
    if not ordered:
        return OutcomeWindowPlan(
            selected=(),
            deferred=(),
            cursor_outcome_key=cursor_outcome_key,
            next_cursor_outcome_key=cursor_outcome_key,
            candle_limit=candle_limit,
            window_start_at=None,
            window_end_at=None,
        )
    start_index = 0
    if cursor_outcome_key:
        for index, due in enumerate(ordered):
            if due.outcome_key == cursor_outcome_key:
                start_index = (index + 1) % len(ordered)
                break
    ring = [*ordered[start_index:], *ordered[:start_index]]
    selected: list[DueInstrument] = []
    earliest_target: dt.datetime | None = None
    latest_deadline: dt.datetime | None = None
    for due in ring:
        candidate_earliest = min(earliest_target, due.target_at) if earliest_target else due.target_at
        due_deadline = due.target_at + dt.timedelta(seconds=max_delay_seconds)
        candidate_latest = max(latest_deadline, due_deadline) if latest_deadline else due_deadline
        window_start = candidate_earliest - dt.timedelta(seconds=INTERVAL_SECONDS)
        requested_candles = int(
            math.ceil((candidate_latest - window_start).total_seconds() / INTERVAL_SECONDS)
        ) + 1
        # A ring wrap would create a disjoint old/new range.  Stop at the first
        # item that no longer fits; the persisted cursor advances next cycle.
        if requested_candles > candle_limit:
            break
        selected.append(due)
        earliest_target = candidate_earliest
        latest_deadline = candidate_latest
    selected_keys = {item.outcome_key for item in selected}
    deferred = tuple(item for item in ordered if item.outcome_key not in selected_keys)
    return OutcomeWindowPlan(
        selected=tuple(selected),
        deferred=deferred,
        cursor_outcome_key=cursor_outcome_key,
        next_cursor_outcome_key=(
            selected[-1].outcome_key if selected else cursor_outcome_key
        ),
        candle_limit=candle_limit,
        window_start_at=(
            earliest_target - dt.timedelta(seconds=INTERVAL_SECONDS)
            if earliest_target
            else None
        ),
        window_end_at=latest_deadline,
    )


def _coerce_due(value: DueInstrument | Mapping[str, object]) -> DueInstrument | None:
    if isinstance(value, DueInstrument):
        due = value
    elif isinstance(value, Mapping):
        target_at = _utc(value.get("target_at"))
        try:
            horizon = int(value.get("horizon_minutes") or value.get("horizon") or 0)
        except (TypeError, ValueError):
            return None
        if target_at is None:
            return None
        funding_window_start_at = _utc(value.get("funding_window_start_at"))
        due = DueInstrument(
            outcome_key=str(value.get("outcome_key") or "").strip(),
            venue=str(value.get("venue") or ""),
            instrument_id=str(value.get("instrument_id") or value.get("inst_id") or ""),
            symbol=str(
                value.get("symbol")
                or str(value.get("instrument_id") or value.get("inst_id") or "").split(":", 1)[-1]
            ),
            market_surface=str(value.get("market_surface") or value.get("market_type") or ""),
            target_at=target_at,
            horizon_minutes=horizon,
            requires_funding_events=bool(
                value.get("requires_funding_events")
                or value.get("requires_realized_funding_events")
            ),
            funding_window_start_at=funding_window_start_at,
            due_window_key=(
                str(value.get("due_window_key"))
                if value.get("due_window_key") not in (None, "")
                else None
            ),
            due_window_start_at=_utc(value.get("due_window_start_at")),
            due_window_end_at=_utc(value.get("due_window_end_at")),
            due_window_max_candles=(
                int(value.get("due_window_max_candles"))
                if value.get("due_window_max_candles") not in (None, "")
                else None
            ),
        )
    else:
        return None
    target = _utc(due.target_at)
    if target is None:
        return None
    funding_start = _utc(due.funding_window_start_at)
    return dataclasses.replace(
        due,
        target_at=target,
        funding_window_start_at=funding_start,
        due_window_start_at=_utc(due.due_window_start_at),
        due_window_end_at=_utc(due.due_window_end_at),
    )


def _identity_reason(request: CandleRequest, fetched: CandleFetch) -> str | None:
    if fetched.response_venue and canonical_venue(fetched.response_venue) != request.venue:
        return "wrong_response_venue"
    if fetched.response_source_name and str(fetched.response_source_name) != request.source_name:
        return "wrong_response_source"
    if fetched.response_instrument_id:
        response_value = str(fetched.response_instrument_id)
        response_symbol = response_value.split(":", 1)[-1]
        if _symbol_token(response_symbol) != _symbol_token(request.symbol):
            return "wrong_response_instrument"
        if ":" in response_value:
            response_venue = canonical_venue(response_value.split(":", 1)[0])
            if response_venue != request.venue:
                return "wrong_response_instrument"

    payload = fetched.payload
    if isinstance(payload, Mapping):
        embedded: list[object] = [payload.get("instId"), payload.get("symbol")]
        arg = payload.get("arg")
        if isinstance(arg, Mapping):
            embedded.extend((arg.get("instId"), arg.get("symbol")))
        result = payload.get("result")
        if isinstance(result, Mapping):
            embedded.extend((result.get("instId"), result.get("symbol")))
            category = result.get("category")
            if request.source.category and category and str(category).lower() != request.source.category.lower():
                return "wrong_response_market_surface"
        for identity in embedded:
            if identity not in (None, "") and _symbol_token(identity) != _symbol_token(request.symbol):
                return "wrong_response_instrument"
    return None


def _timestamp(value: object, *, milliseconds: bool) -> dt.datetime | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if milliseconds:
        numeric /= 1000.0
    try:
        return dt.datetime.fromtimestamp(numeric, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_candle_fetch(request: CandleRequest, fetched: CandleFetch) -> ParsedCandleBatch:
    """Parse one response and retain only demonstrably closed one-minute bars."""

    if not fetched.ok:
        return ParsedCandleBatch((), (), 0, "fetch_failed")
    received_at = _utc(fetched.received_at)
    if received_at is None:
        return ParsedCandleBatch((), (), 0, "invalid_received_at")
    identity_reason = _identity_reason(request, fetched)
    if identity_reason:
        return ParsedCandleBatch((), (), 0, identity_reason)

    parser = request.parser
    payload = fetched.payload
    if parser == "okx_1m_candles":
        if not isinstance(payload, Mapping) or str(payload.get("code", "0")) != "0":
            return ParsedCandleBatch((), (), 0, "source_payload_error")
        raw_rows = payload.get("data") or []
    elif parser == "bybit_v5_1m_klines":
        if not isinstance(payload, Mapping) or int(payload.get("retCode", 0) or 0) != 0:
            return ParsedCandleBatch((), (), 0, "source_payload_error")
        result = payload.get("result")
        raw_rows = result.get("list") or [] if isinstance(result, Mapping) else []
    else:
        raw_rows = payload
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
        return ParsedCandleBatch((), (), 0, "source_payload_error")

    completed: dict[dt.datetime, ClosedCandle] = {}
    conflicts: set[dt.datetime] = set()
    partial_event_ats: list[dt.datetime] = []
    invalid_count = 0
    for raw in raw_rows:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            invalid_count += 1
            continue
        open_at: dt.datetime | None = None
        event_at: dt.datetime | None = None
        price: float | None = None
        explicit_complete = True
        if parser == "okx_1m_candles" and len(raw) >= 9:
            open_at = _timestamp(raw[0], milliseconds=True)
            event_at = open_at + dt.timedelta(seconds=INTERVAL_SECONDS) if open_at else None
            price = _positive_float(raw[4])
            explicit_complete = str(raw[8]) == "1"
        elif parser == "gate_1m_candles" and len(raw) >= 3:
            open_at = _timestamp(raw[0], milliseconds=False)
            event_at = open_at + dt.timedelta(seconds=INTERVAL_SECONDS) if open_at else None
            price = _positive_float(raw[2])
        elif parser == "binance_style_1m_klines" and len(raw) >= 7:
            open_at = _timestamp(raw[0], milliseconds=True)
            inclusive_close_at = _timestamp(raw[6], milliseconds=True)
            # Binance/MEXC closeTime is the inclusive final millisecond.  The
            # price becomes a closed-bar observation at the next millisecond.
            event_at = inclusive_close_at + dt.timedelta(milliseconds=1) if inclusive_close_at else None
            price = _positive_float(raw[4])
            if open_at and event_at:
                duration = (event_at - open_at).total_seconds()
                if not 59.0 <= duration <= 61.0:
                    open_at = None
        elif parser == "bybit_v5_1m_klines" and len(raw) >= 5:
            open_at = _timestamp(raw[0], milliseconds=True)
            event_at = open_at + dt.timedelta(seconds=INTERVAL_SECONDS) if open_at else None
            price = _positive_float(raw[4])
        else:
            invalid_count += 1
            continue
        if open_at is None or event_at is None or price is None:
            invalid_count += 1
            continue
        complete = explicit_complete and event_at <= received_at
        if not complete:
            partial_event_ats.append(event_at)
            continue
        candle = ClosedCandle(open_at, event_at, price)
        prior = completed.get(open_at)
        if prior is not None and prior.price != candle.price:
            conflicts.add(open_at)
            completed.pop(open_at, None)
            invalid_count += 1
            continue
        if open_at not in conflicts:
            completed[open_at] = candle

    return ParsedCandleBatch(
        tuple(sorted(completed.values(), key=lambda item: item.event_at)),
        tuple(sorted(partial_event_ats)),
        invalid_count,
    )


def parse_okx_funding_fetch(
    request: FundingRequest, fetched: CandleFetch
) -> ParsedFundingBatch:
    """Normalize settled OKX funding events without using estimates."""

    if not fetched.ok:
        return ParsedFundingBatch((), 0, 0, "fetch_failed")
    received_at = _utc(fetched.received_at)
    if received_at is None:
        return ParsedFundingBatch((), 0, 0, "invalid_received_at")
    if fetched.response_venue and canonical_venue(fetched.response_venue) != request.venue:
        return ParsedFundingBatch((), 0, 0, "wrong_response_venue")
    if fetched.response_source_name and fetched.response_source_name != request.source_name:
        return ParsedFundingBatch((), 0, 0, "wrong_response_source")
    if fetched.response_instrument_id:
        response_symbol = str(fetched.response_instrument_id).split(":", 1)[-1]
        if _symbol_token(response_symbol) != _symbol_token(request.symbol):
            return ParsedFundingBatch((), 0, 0, "wrong_response_instrument")
    payload = fetched.payload
    if not isinstance(payload, Mapping) or str(payload.get("code", "")) != "0":
        return ParsedFundingBatch((), 0, 0, "source_payload_error")
    raw_rows = payload.get("data") or []
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
        return ParsedFundingBatch((), 0, 0, "source_payload_error")

    events_by_time: dict[dt.datetime, RealizedFundingEvent] = {}
    invalid_count = 0
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            invalid_count += 1
            continue
        # The official response includes instId per row.  Missing or mixed
        # identity invalidates the whole fetch rather than being silently skipped.
        row_instrument = raw.get("instId")
        if row_instrument in (None, "") or _symbol_token(row_instrument) != _symbol_token(
            request.symbol
        ):
            return ParsedFundingBatch(
                (), len(raw_rows), invalid_count, "wrong_response_instrument"
            )
        if "realizedRate" not in raw:
            # Never substitute current or estimated fundingRate.
            invalid_count += 1
            continue
        realized_rate = _finite_float(raw.get("realizedRate"))
        event_at = _timestamp(raw.get("fundingTime"), milliseconds=True)
        method = str(raw.get("method") or "").strip()
        formula_type = str(raw.get("formulaType") or "").strip()
        if (
            realized_rate is None
            or event_at is None
            or event_at > received_at
            or not method
            or not formula_type
        ):
            invalid_count += 1
            continue
        event = RealizedFundingEvent(
            event_at=event_at,
            realized_rate=realized_rate,
            method=method,
            formula_type=formula_type,
        )
        prior = events_by_time.get(event_at)
        if prior is not None and prior != event:
            return ParsedFundingBatch(
                (), len(raw_rows), invalid_count + 1, "conflicting_funding_event"
            )
        events_by_time[event_at] = event
    return ParsedFundingBatch(
        tuple(sorted(events_by_time.values(), key=lambda item: item.event_at)),
        len(raw_rows),
        invalid_count,
    )


def _funding_request_for_group(
    dues: list[DueInstrument], source: FundingSource, max_delay_seconds: float
) -> FundingRequest | str:
    first = dues[0]
    venue = canonical_venue(first.venue)
    surface = canonical_market_surface(first.market_surface)
    symbol = canonical_symbol(first.symbol)
    instrument_id = canonical_instrument_id(venue, first.instrument_id, symbol)
    if venue != "OKX" or surface != "perpetual_swap" or not symbol.endswith("-SWAP"):
        return "funding_source_unqualified"
    starts = [
        item.funding_window_start_at
        or item.target_at - dt.timedelta(minutes=int(item.horizon_minutes))
        for item in dues
    ]
    window_start = min(starts)
    window_end = max(
        item.target_at + dt.timedelta(seconds=max_delay_seconds) for item in dues
    )
    try:
        url = source.url_template.format(
            symbol=urllib.parse.quote(symbol, safe="-_"),
        )
    except (KeyError, ValueError):
        return "invalid_funding_source_url_template"
    parsed_url = urllib.parse.urlsplit(url)
    if (
        parsed_url.scheme.lower() != "https"
        or parsed_url.hostname != "www.okx.com"
        or parsed_url.path != "/api/v5/public/funding-rate-history"
        or parsed_url.username
        or parsed_url.password
        or source.endpoint != "/api/v5/public/funding-rate-history"
        or source.parser != "okx_realized_funding_history"
    ):
        return "unsafe_public_source_url"
    return FundingRequest(
        venue=venue,
        instrument_id=instrument_id,
        symbol=symbol,
        market_surface=surface,
        source_name=source.source_name,
        parser=source.parser,
        url=url,
        window_start_at=window_start,
        window_end_at=window_end,
        outcome_keys=tuple(sorted(item.outcome_key for item in dues)),
        source=source,
    )


def _request_for_group(
    dues: list[DueInstrument], source: CandleSource, max_delay_seconds: float
) -> CandleRequest | str:
    first = dues[0]
    venue = canonical_venue(first.venue)
    surface = canonical_market_surface(first.market_surface)
    symbol = canonical_symbol(first.symbol)
    instrument_id = canonical_instrument_id(venue, first.instrument_id, symbol)
    if not venue or not surface or not symbol or not instrument_id:
        return "invalid_request_identity"
    if source.key != (venue, surface):
        return "source_identity_mismatch"
    raw_id = str(first.instrument_id or "")
    if ":" in raw_id and canonical_venue(raw_id.split(":", 1)[0]) != venue:
        return "instrument_venue_mismatch"
    if raw_id and _symbol_token(raw_id.split(":", 1)[-1]) != _symbol_token(symbol):
        return "instrument_symbol_mismatch"
    if source.parser not in SUPPORTED_PARSERS:
        return "unsupported_candle_parser"

    earliest_target = min(item.target_at for item in dues)
    latest_deadline = max(
        item.target_at + dt.timedelta(seconds=max_delay_seconds) for item in dues
    )
    window_start = earliest_target - dt.timedelta(seconds=INTERVAL_SECONDS)
    requested_candles = int(
        math.ceil((latest_deadline - window_start).total_seconds() / INTERVAL_SECONDS)
    ) + 1
    maximum_candles = min(
        HARD_MAX_CANDLES_PER_REQUEST,
        max(1, int(source.max_candles)),
    )
    if requested_candles > maximum_candles:
        return "target_span_exceeds_single_fetch_window"
    limit = min(maximum_candles, max(2, requested_candles))
    encoded_symbol = urllib.parse.quote(symbol, safe="-_")
    substitutions = {
        "symbol": encoded_symbol,
        "start_ms": int(window_start.timestamp() * 1000),
        "end_ms": int(latest_deadline.timestamp() * 1000),
        "start_seconds": int(window_start.timestamp()),
        "end_seconds": int(latest_deadline.timestamp()),
        "limit": limit,
    }
    try:
        url = source.url_template.format(**substitutions)
    except (KeyError, ValueError):
        return "invalid_source_url_template"
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.scheme.lower() != "https" or parsed_url.username or parsed_url.password:
        return "unsafe_public_source_url"
    return CandleRequest(
        venue=venue,
        instrument_id=instrument_id,
        symbol=symbol,
        market_surface=surface,
        source_name=str(source.source_name),
        parser=source.parser,
        url=url,
        window_start_at=window_start,
        window_end_at=latest_deadline,
        source=source,
    )


def _rejection(due: DueInstrument, reason: str) -> dict[str, object]:
    return {
        "outcome_key": due.outcome_key,
        "horizon_minutes": due.horizon_minutes,
        "venue": canonical_venue(due.venue),
        "inst_id": canonical_instrument_id(due.venue, due.instrument_id, due.symbol),
        "target_at": _iso(due.target_at),
        "reason": reason,
    }


def _select_record(
    due: DueInstrument,
    request: CandleRequest,
    fetched: CandleFetch,
    parsed: ParsedCandleBatch,
    max_delay_seconds: float,
) -> tuple[dict[str, object] | None, str | None]:
    if parsed.fatal_reason:
        return None, parsed.fatal_reason
    deadline = due.target_at + dt.timedelta(seconds=max_delay_seconds)
    eligible = [
        candle
        for candle in parsed.candles
        if due.target_at <= candle.event_at <= deadline
    ]
    if not eligible:
        if any(due.target_at <= value <= deadline for value in parsed.partial_event_ats):
            return None, "partial_candle"
        if parsed.candles and max(item.event_at for item in parsed.candles) < due.target_at:
            return None, "stale_candle"
        if parsed.candles and min(item.event_at for item in parsed.candles) > deadline:
            return None, "candle_after_deadline"
        return None, "no_closed_candle_in_window"
    candle = min(eligible, key=lambda item: item.event_at)
    received_at = _utc(fetched.received_at)
    if received_at is None:
        return None, "invalid_received_at"
    source_identity = (
        f"{request.source_name}|{request.venue}|{request.instrument_id}|"
        f"{request.market_surface}|1m"
    )
    source_event_id = f"{source_identity}|{_iso(candle.candle_open_at)}"
    source_endpoint = urllib.parse.urlsplit(request.url).path
    return (
        {
            "outcome_key": due.outcome_key,
            "horizon_minutes": int(due.horizon_minutes),
            "target_at": _iso(due.target_at),
            "delay_seconds": round((candle.event_at - due.target_at).total_seconds(), 3),
            "source_kind": SOURCE_KIND,
            "venue": request.venue,
            "inst_id": request.instrument_id,
            "symbol": request.symbol,
            "market_surface": request.market_surface,
            "event_at": _iso(candle.event_at),
            "received_at": _iso(received_at),
            "price": candle.price,
            "source_name": request.source_name,
            "source_parser": request.parser,
            "source_endpoint": source_endpoint,
            "source_event_id": source_event_id,
            "source_identity": source_identity,
            "source_url": request.url,
            "candle_open_at": _iso(candle.candle_open_at),
            "interval_seconds": INTERVAL_SECONDS,
            "is_closed": True,
            "is_partial": False,
            "freshness_state": "fresh",
            "quality_status": QUALITY_STATUS,
            "measurement_scope": "single_leg_price_observation",
            "paired_outcome_complete": False,
        },
        None,
    )


def _funding_records_and_coverage(
    dues: list[DueInstrument],
    request: FundingRequest,
    fetched: CandleFetch,
    parsed: ParsedFundingBatch,
    max_delay_seconds: float,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    received_at = _utc(fetched.received_at)
    payload_json = json.dumps(
        fetched.payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    query_identity = {
        "request_url": request.url,
        "requested_from": _iso(request.window_start_at),
        "requested_through": _iso(request.window_end_at),
        "received_at": _iso(received_at) if received_at else None,
        "payload_sha256": payload_sha256,
    }
    query_id = "okx-funding-query-" + hashlib.sha256(
        json.dumps(query_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    try:
        http_status = int(str(fetched.http_status or "0"))
    except ValueError:
        http_status = 0
    base_coverage: dict[str, object] = {
        "venue": request.venue,
        "inst_id": request.instrument_id,
        "market_surface": request.market_surface,
        "source_name": request.source_name,
        "requested_outcome_keys": list(request.outcome_keys),
        "window_start_at": _iso(request.window_start_at),
        "window_end_at": _iso(request.window_end_at),
        "received_at": _iso(received_at) if received_at else None,
        "raw_row_count": parsed.raw_row_count,
        "invalid_row_count": parsed.invalid_row_count,
        "paired_outcome_complete": False,
        "allow_estimates": False,
        "source": {
            "name": request.source_name,
            "endpoint": request.source.endpoint,
            "parser": request.parser,
            "inst_id": request.instrument_id,
        },
        "query": {
            "query_id": query_id,
            "request_url": request.url,
            "requested_from": _iso(request.window_start_at),
            "requested_through": _iso(request.window_end_at),
            "received_at": _iso(received_at) if received_at else None,
            "request_succeeded": False,
            "http_status": http_status,
            "page_count": 1 if fetched.ok else 0,
            "pagination_complete": False,
            "range_complete": False,
            "payload_sha256": payload_sha256,
        },
        "events": [],
    }
    if parsed.fatal_reason:
        return [], {
            **base_coverage,
            "coverage_status": "rejected",
            "reason": parsed.fatal_reason,
            "accepted_event_count": 0,
        }

    relevant = [
        event
        for event in parsed.events
        if request.window_start_at < event.event_at <= request.window_end_at
    ]
    source_identity = (
        f"{request.source_name}|{request.venue}|{request.instrument_id}|"
        f"{request.market_surface}|realized_funding"
    )
    output: list[dict[str, object]] = []
    for event in relevant:
        covered_keys = []
        for due in dues:
            due_start = due.funding_window_start_at or (
                due.target_at - dt.timedelta(minutes=int(due.horizon_minutes))
            )
            due_end = due.target_at + dt.timedelta(seconds=max_delay_seconds)
            if due_start < event.event_at <= due_end:
                covered_keys.append(due.outcome_key)
        source_event_id = f"{source_identity}|{_iso(event.event_at)}"
        output.append(
            {
                "outcome_keys": sorted(covered_keys),
                "source_kind": FUNDING_SOURCE_KIND,
                "venue": request.venue,
                "inst_id": request.instrument_id,
                "symbol": request.symbol,
                "market_surface": request.market_surface,
                "event_at": _iso(event.event_at),
                "received_at": _iso(received_at) if received_at else None,
                "realized_rate": event.realized_rate,
                "method": event.method,
                "formula_type": event.formula_type,
                "estimated": False,
                "source_name": request.source_name,
                "source_event_id": source_event_id,
                "source_identity": source_identity,
                "source_parser": request.parser,
                "source_endpoint": request.source.endpoint,
                "is_settled": True,
                "quality_status": QUALITY_STATUS,
                "measurement_scope": "single_leg_realized_funding_event",
                "paired_outcome_complete": False,
                "short_perp_contribution_formula": (
                    "+perp_notional*realized_rate"
                ),
            }
        )

    earliest = min((event.event_at for event in parsed.events), default=None)
    latest = max((event.event_at for event in parsed.events), default=None)
    left_boundary_covered = bool(
        earliest is not None and earliest <= request.window_start_at
    ) or parsed.raw_row_count < request.source.max_events
    right_boundary_observable = bool(
        received_at is not None and received_at >= request.window_end_at
    )
    request_succeeded = bool(fetched.ok and http_status == 200)
    pagination_complete = bool(request_succeeded and left_boundary_covered)
    range_complete = (
        request_succeeded
        and parsed.invalid_row_count == 0
        and pagination_complete
        and right_boundary_observable
    )
    if not request_succeeded:
        reason = "funding_request_not_verified"
    elif not right_boundary_observable:
        reason = "funding_window_not_yet_observable"
    elif not left_boundary_covered:
        reason = "funding_history_window_truncated"
    elif parsed.invalid_row_count:
        reason = "invalid_funding_rows_present"
    else:
        reason = "qualified_realized_funding_history"
    coverage_query = dict(base_coverage["query"])
    coverage_query.update(
        {
            "request_succeeded": request_succeeded,
            "pagination_complete": pagination_complete,
            "range_complete": range_complete,
        }
    )
    return output, {
        **base_coverage,
        "coverage_status": "complete" if range_complete else "incomplete",
        "reason": reason,
        "accepted_event_count": len(output),
        "complete_from": (
            _iso(request.window_start_at)
            if pagination_complete
            else _iso(earliest) if earliest else None
        ),
        "complete_through": (
            _iso(request.window_end_at)
            if right_boundary_observable
            else _iso(received_at) if received_at else None
        ),
        "earliest_event_at": _iso(earliest) if earliest else None,
        "latest_event_at": _iso(latest) if latest else None,
        "left_boundary_covered": left_boundary_covered,
        "right_boundary_observable": right_boundary_observable,
        "query": coverage_query,
        "events": output,
    }
def _effective_rate(
    source: CandleSource | FundingSource, config: CollectorConfig
) -> float:
    rate = max(0.01, float(source.rate_limit_per_second))
    rate_key = canonical_venue(source.rate_limit_key or source.venue)
    return (
        min(rate, 8.0, max(0.01, float(config.okx_max_requests_per_second)))
        if rate_key == "OKX"
        else rate
    )


def collect_due_outcome_prices(
    provider: DueInstrumentProvider,
    sources: Iterable[CandleSource] | None = None,
    *,
    fetcher: PublicCandleFetcher | None = None,
    config: CollectorConfig | None = None,
    pacer: VenueRatePacer | None = None,
    settings: Mapping[str, object] | None = None,
    funding_source: FundingSource | None = None,
    window_cursors: Mapping[object, str] | None = None,
) -> dict[str, object]:
    """Fetch at most one public candle response per unique venue/instrument."""

    if config is not None and settings is not None:
        raise ValueError("pass_config_or_settings_not_both")
    config = config or (
        collector_config_from_settings(settings) if settings is not None else CollectorConfig()
    )
    fetcher = fetcher or UrllibPublicCandleFetcher()
    pacer = pacer or VenueRatePacer()
    funding_source = funding_source or FundingSource()
    window_cursors = window_cursors or {}
    maximum_instruments = min(HARD_MAX_INSTRUMENTS, max(0, int(config.max_instruments)))
    maximum_workers = min(HARD_MAX_WORKERS, max(1, int(config.max_workers)))
    request_timeout = min(
        HARD_MAX_REQUEST_TIMEOUT_SECONDS,
        max(0.1, float(config.request_timeout_seconds)),
    )
    collection_timeout = min(
        HARD_MAX_COLLECTION_TIMEOUT_SECONDS,
        max(0.1, float(config.collection_timeout_seconds)),
    )
    max_delay = min(300.0, max(0.0, float(config.max_delay_seconds)))
    allowed_horizons = {int(value) for value in config.allowed_horizon_minutes}
    source_lookup = {source.key: source for source in (sources or default_public_candle_sources())}

    if not config.enabled:
        return {
            "records": [],
            "funding_events": [],
            "funding_coverage": [],
            "window_plans": [],
            "deferred_outcome_keys": [],
            "attempted_window_keys": [],
            "rejections": [],
            "loaded_due_count": 0,
            "unique_instrument_count": 0,
            "fetched_instrument_count": 0,
            "enabled": False,
            "reason": "paper_due_outcome_collection_disabled",
            "limits": {
                "max_instruments": maximum_instruments,
                "max_workers": maximum_workers,
                "request_timeout_seconds": request_timeout,
                "collection_timeout_seconds": collection_timeout,
                "max_delay_seconds": max_delay,
            },
        }

    rejections: list[dict[str, object]] = []
    dues: list[DueInstrument] = []
    seen_outcome_keys: set[str] = set()
    try:
        loaded = provider.load_due_instruments(limit=maximum_instruments)
        for raw in loaded:
            due = _coerce_due(raw)
            if due is None:
                rejections.append({"outcome_key": None, "reason": "invalid_due_instrument"})
                continue
            if not due.outcome_key:
                rejections.append(_rejection(due, "missing_outcome_key"))
                continue
            if due.outcome_key in seen_outcome_keys:
                rejections.append(_rejection(due, "duplicate_outcome_key"))
                continue
            seen_outcome_keys.add(due.outcome_key)
            if due.horizon_minutes not in allowed_horizons:
                rejections.append(_rejection(due, "horizon_not_allowed"))
                continue
            dues.append(due)
    except Exception as exc:  # noqa: BLE001 - provider failures must fail closed
        return {
            "records": [],
            "funding_events": [],
            "funding_coverage": [],
            "window_plans": [],
            "deferred_outcome_keys": [],
            "attempted_window_keys": [],
            "rejections": [
                {"outcome_key": None, "reason": "due_provider_failed", "error": type(exc).__name__}
            ],
            "loaded_due_count": 0,
            "unique_instrument_count": 0,
            "fetched_instrument_count": 0,
            "limits": {
                "max_instruments": maximum_instruments,
                "max_workers": maximum_workers,
                "request_timeout_seconds": request_timeout,
                "collection_timeout_seconds": collection_timeout,
                "max_delay_seconds": max_delay,
            },
        }

    grouped: dict[tuple[str, str], list[DueInstrument]] = defaultdict(list)
    accepted_group_identity: dict[tuple[str, str], tuple[str, str]] = {}
    for due in dues:
        venue = canonical_venue(due.venue)
        canonical_id = canonical_instrument_id(venue, due.instrument_id, due.symbol)
        group_key = venue, canonical_id
        identity = canonical_symbol(due.symbol), canonical_market_surface(due.market_surface)
        if not venue or not canonical_id or not all(identity):
            rejections.append(_rejection(due, "invalid_request_identity"))
            continue
        if group_key not in grouped and len(grouped) >= maximum_instruments:
            rejections.append(_rejection(due, "instrument_limit_exceeded"))
            continue
        if group_key in accepted_group_identity and accepted_group_identity[group_key] != identity:
            rejections.append(_rejection(due, "conflicting_due_instrument_identity"))
            continue
        accepted_group_identity[group_key] = identity
        grouped[group_key].append(due)

    requests: dict[tuple[str, str], CandleRequest] = {}
    request_dues_by_group: dict[tuple[str, str], list[DueInstrument]] = {}
    window_plans: list[dict[str, object]] = []
    deferred_outcome_keys: list[str] = []
    attempted_window_keys: list[str] = []
    for group_key, group_dues in grouped.items():
        surface = canonical_market_surface(group_dues[0].market_surface)
        source = source_lookup.get((group_key[0], surface))
        if source is None:
            rejections.extend(_rejection(item, "unqualified_candle_source") for item in group_dues)
            continue
        provider_window_keys = {
            str(item.due_window_key)
            for item in group_dues
            if item.due_window_key not in (None, "")
        }
        if provider_window_keys:
            attempted_window_keys.extend(sorted(provider_window_keys))
            provider_limits = {
                int(item.due_window_max_candles or 0) for item in group_dues
            }
            provider_starts = {item.due_window_start_at for item in group_dues}
            provider_ends = {item.due_window_end_at for item in group_dues}
            valid_provider_window = (
                len(provider_window_keys) == 1
                and all(item.due_window_key for item in group_dues)
                and len(provider_limits) == 1
                and 0 < next(iter(provider_limits)) <= HARD_MAX_CANDLES_PER_REQUEST
                and len(provider_starts) == 1
                and None not in provider_starts
                and len(provider_ends) == 1
                and None not in provider_ends
                and all(
                    item.due_window_start_at <= item.target_at <= item.due_window_end_at
                    for item in group_dues
                )
            )
            if not valid_provider_window:
                rejections.extend(
                    _rejection(item, "invalid_provider_due_window")
                    for item in group_dues
                )
                window_plans.append(
                    {
                        "venue": group_key[0],
                        "inst_id": group_key[1],
                        "provider_planned": True,
                        "due_window_keys": sorted(provider_window_keys),
                        "selected_outcome_keys": [],
                        "deferred_outcome_keys": [],
                        "reason": "invalid_provider_due_window",
                    }
                )
                continue
            selected_dues = list(group_dues)
            deferred_keys: list[str] = []
            window_plans.append(
                {
                    "venue": group_key[0],
                    "inst_id": group_key[1],
                    "provider_planned": True,
                    "due_window_key": next(iter(provider_window_keys)),
                    "selected_outcome_keys": [item.outcome_key for item in group_dues],
                    "deferred_outcome_keys": [],
                    "candle_limit": next(iter(provider_limits)),
                    "window_start_at": _iso(next(iter(provider_starts))),
                    "window_end_at": _iso(next(iter(provider_ends))),
                }
            )
        else:
            serialized_cursor_key = f"{group_key[0]}|{group_key[1]}"
            cursor = window_cursors.get(group_key) or window_cursors.get(serialized_cursor_key)
            plan = plan_due_instrument_window(
                group_dues,
                source,
                max_delay_seconds=max_delay,
                cursor_outcome_key=str(cursor) if cursor else None,
            )
            selected_dues = list(plan.selected)
            deferred_keys = [item.outcome_key for item in plan.deferred]
            deferred_outcome_keys.extend(deferred_keys)
            window_plans.append(
                {
                    "venue": group_key[0],
                    "inst_id": group_key[1],
                    "provider_planned": False,
                    "cursor_outcome_key": plan.cursor_outcome_key,
                    "next_cursor_outcome_key": plan.next_cursor_outcome_key,
                    "selected_outcome_keys": [item.outcome_key for item in plan.selected],
                    "deferred_outcome_keys": deferred_keys,
                    "candle_limit": plan.candle_limit,
                    "window_start_at": _iso(plan.window_start_at) if plan.window_start_at else None,
                    "window_end_at": _iso(plan.window_end_at) if plan.window_end_at else None,
                }
            )
        if not selected_dues:
            continue
        request = _request_for_group(selected_dues, source, max_delay)
        if isinstance(request, str):
            rejections.extend(_rejection(item, request) for item in selected_dues)
            continue
        requests[group_key] = request
        request_dues_by_group[group_key] = selected_dues

    funding_requests: dict[tuple[str, str], FundingRequest] = {}
    funding_dues_by_group: dict[tuple[str, str], list[DueInstrument]] = {}
    funding_coverage: list[dict[str, object]] = []
    for group_key, group_dues in request_dues_by_group.items():
        paired_dues = [item for item in group_dues if item.requires_funding_events]
        if not paired_dues:
            continue
        funding_dues_by_group[group_key] = paired_dues
        request = _funding_request_for_group(paired_dues, funding_source, max_delay)
        if isinstance(request, str):
            funding_coverage.append(
                {
                    "venue": group_key[0],
                    "inst_id": group_key[1],
                    "requested_outcome_keys": sorted(item.outcome_key for item in paired_dues),
                    "coverage_status": "rejected",
                    "reason": request,
                    "accepted_event_count": 0,
                    "paired_outcome_complete": False,
                }
            )
            continue
        funding_requests[group_key] = request

    deadline = time.monotonic() + collection_timeout

    JobKey = tuple[str, str, str]

    def fetch_one(
        job_key: JobKey, request: CandleRequest | FundingRequest
    ) -> tuple[JobKey, CandleFetch]:
        rate_key = canonical_venue(request.source.rate_limit_key or request.source.venue)
        if not pacer.acquire(rate_key, _effective_rate(request.source, config), deadline):
            return job_key, CandleFetch(
                ok=False,
                payload=None,
                received_at=dt.datetime.now(UTC),
                error="collection_deadline_exceeded_before_fetch",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return job_key, CandleFetch(
                ok=False,
                payload=None,
                received_at=dt.datetime.now(UTC),
                error="collection_deadline_exceeded_before_fetch",
            )
        try:
            return job_key, fetcher.fetch(
                request, timeout_seconds=min(request_timeout, max(0.1, remaining))
            )
        except Exception as exc:  # noqa: BLE001 - one venue must not abort the bounded batch
            return job_key, CandleFetch(
                ok=False,
                payload=None,
                received_at=dt.datetime.now(UTC),
                error=f"fetcher_exception:{type(exc).__name__}",
            )

    jobs: dict[JobKey, CandleRequest | FundingRequest] = {
        ("candle", *group_key): request for group_key, request in requests.items()
    }
    jobs.update(
        {
            ("funding", *group_key): request
            for group_key, request in funding_requests.items()
        }
    )
    fetched_by_job: dict[JobKey, CandleFetch] = {}
    timed_out_jobs: set[JobKey] = set()
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(maximum_workers, len(jobs) or 1)
    )
    try:
        futures = {
            pool.submit(fetch_one, job_key, request): job_key
            for job_key, request in jobs.items()
        }
        done, pending = concurrent.futures.wait(
            futures,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        for future in done:
            job_key = futures[future]
            try:
                returned_key, fetched = future.result()
            except Exception:  # pragma: no cover - fetch_one contains its own isolation
                timed_out_jobs.add(job_key)
                continue
            if returned_key != job_key:
                timed_out_jobs.add(job_key)
                continue
            fetched_by_job[job_key] = fetched
        for future in pending:
            timed_out_jobs.add(futures[future])
            future.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    records: list[dict[str, object]] = []
    for group_key, request in requests.items():
        group_dues = request_dues_by_group[group_key]
        candle_job_key = ("candle", *group_key)
        if candle_job_key in timed_out_jobs:
            rejections.extend(_rejection(item, "collection_timeout") for item in group_dues)
            continue
        fetched = fetched_by_job.get(candle_job_key)
        if fetched is None:
            rejections.extend(_rejection(item, "fetch_failed") for item in group_dues)
            continue
        parsed = parse_candle_fetch(request, fetched)
        for due in group_dues:
            record, reason = _select_record(due, request, fetched, parsed, max_delay)
            if record is not None:
                records.append(record)
            else:
                rejections.append(_rejection(due, str(reason or "candle_rejected")))

    funding_events: list[dict[str, object]] = []
    for group_key, request in funding_requests.items():
        funding_job_key = ("funding", *group_key)
        group_dues = funding_dues_by_group[group_key]
        if funding_job_key in timed_out_jobs:
            funding_coverage.append(
                {
                    "venue": request.venue,
                    "inst_id": request.instrument_id,
                    "requested_outcome_keys": list(request.outcome_keys),
                    "coverage_status": "rejected",
                    "reason": "collection_timeout",
                    "accepted_event_count": 0,
                    "paired_outcome_complete": False,
                }
            )
            continue
        fetched = fetched_by_job.get(funding_job_key)
        if fetched is None:
            funding_coverage.append(
                {
                    "venue": request.venue,
                    "inst_id": request.instrument_id,
                    "requested_outcome_keys": list(request.outcome_keys),
                    "coverage_status": "rejected",
                    "reason": "fetch_failed",
                    "accepted_event_count": 0,
                    "paired_outcome_complete": False,
                }
            )
            continue
        parsed = parse_okx_funding_fetch(request, fetched)
        new_events, coverage = _funding_records_and_coverage(
            group_dues, request, fetched, parsed, max_delay
        )
        funding_events.extend(new_events)
        funding_coverage.append(coverage)

    records.sort(key=lambda item: (str(item["target_at"]), str(item["outcome_key"])))
    funding_events.sort(
        key=lambda item: (str(item["event_at"]), str(item["source_event_id"]))
    )
    funding_coverage.sort(
        key=lambda item: (str(item.get("venue") or ""), str(item.get("inst_id") or ""))
    )
    window_plans.sort(
        key=lambda item: (str(item.get("venue") or ""), str(item.get("inst_id") or ""))
    )
    rejections.sort(key=lambda item: (str(item.get("outcome_key") or ""), str(item.get("reason") or "")))
    return {
        "records": records,
        "funding_events": funding_events,
        "funding_coverage": funding_coverage,
        "window_plans": window_plans,
        "deferred_outcome_keys": sorted(deferred_outcome_keys),
        "attempted_window_keys": sorted(set(attempted_window_keys)),
        "rejections": rejections,
        "loaded_due_count": len(dues),
        "unique_instrument_count": len(grouped),
        "fetched_instrument_count": sum(
            1 for job_key in fetched_by_job if job_key[0] == "candle"
        ),
        "funding_fetch_count": sum(
            1 for job_key in fetched_by_job if job_key[0] == "funding"
        ),
        "total_public_request_count": len(fetched_by_job),
        "enabled": True,
        "limits": {
            "max_instruments": maximum_instruments,
            "max_workers": maximum_workers,
            "request_timeout_seconds": request_timeout,
            "collection_timeout_seconds": collection_timeout,
            "max_delay_seconds": max_delay,
            "allowed_horizon_minutes": sorted(allowed_horizons),
            "okx_max_requests_per_second": min(
                8.0, float(config.okx_max_requests_per_second)
            ),
            "allow_latest_ticker_fallback": False,
        },
    }


__all__ = [
    "CandleFetch",
    "CandleRequest",
    "CandleSource",
    "CollectorConfig",
    "DueInstrument",
    "DueInstrumentProvider",
    "FundingRequest",
    "FundingSource",
    "OutcomeWindowPlan",
    "ParsedCandleBatch",
    "ParsedFundingBatch",
    "PublicCandleFetcher",
    "UrllibPublicCandleFetcher",
    "VenueRatePacer",
    "canonical_instrument_id",
    "canonical_market_surface",
    "canonical_symbol",
    "canonical_venue",
    "collect_due_outcome_prices",
    "collector_config_from_settings",
    "default_public_candle_sources",
    "outcome_measurement_capability",
    "plan_due_instrument_window",
    "parse_candle_fetch",
    "parse_okx_funding_fetch",
]
