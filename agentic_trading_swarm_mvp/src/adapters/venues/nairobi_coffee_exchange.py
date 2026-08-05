"""Nairobi Coffee Exchange public grade-market-report adapter.

Nairobi Coffee Exchange publishes its auction market totals as public PDF
reports.  The reports are useful, priceable evidence of auction-grade
dispersion, but do not offer an anonymous order endpoint.  This adapter keeps
the published prices available for synthetic paper research while exposing no
live execution route.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, health_observation, utc_now
from scan_batch import ScanBatch


MARKET_REPORT_URL = "https://www.nairobicoffeeexchange.co.ke/api/market-reports/3/pdf"
SOURCE_URL = MARKET_REPORT_URL
ABOUT_URL = "https://www.nairobicoffeeexchange.co.ke/about"
VENUE = "NAIROBI_COFFEE_EXCHANGE"
MARKET_SURFACE = "nairobi_coffee_exchange_auction_grade_market_totals"
GRADE_CODES = ("AA", "AB", "C", "E", "HE", "MH", "ML", "NH", "NL", "PB", "SB", "T", "TT", "UG", "UG1", "UG2", "UG3")


class NairobiCoffeeExchangeParseError(ValueError):
    """Raised when a reachable report no longer contains a usable market table."""


def extract_pdf_text(body: bytes) -> str:
    """Extract visible text from the exchange's bounded public PDF report."""

    if not isinstance(body, bytes) or not body:
        raise NairobiCoffeeExchangeParseError("Nairobi Coffee Exchange market report PDF is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise NairobiCoffeeExchangeParseError(
            "pypdf is required to read Nairobi Coffee Exchange market report PDFs"
        ) from exc
    try:
        text = "\n".join(str(page.extract_text() or "") for page in PdfReader(io.BytesIO(body)).pages)
    except Exception as exc:  # noqa: BLE001 - source revisions must be retained as health evidence.
        raise NairobiCoffeeExchangeParseError(f"market report PDF could not be read: {exc}") from exc
    if not text.strip():
        raise NairobiCoffeeExchangeParseError("market report PDF contains no extractable text")
    return text


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NairobiCoffeeExchangeParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _number(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except (AttributeError, ValueError) as exc:
        raise NairobiCoffeeExchangeParseError(f"invalid market-report numeric value: {value!r}") from exc


def _report_session(text: str) -> tuple[int, dt.date]:
    match = re.search(
        r"\bSale\s+(\d+)\s+of\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise NairobiCoffeeExchangeParseError("auction sale number and date were not found")
    try:
        return int(match.group(1)), dt.datetime.strptime(match.group(2), "%B %d, %Y").date()
    except ValueError as exc:
        raise NairobiCoffeeExchangeParseError("auction sale date is invalid") from exc


def _grade_rows(text: str) -> list[tuple[str, float, float, float, float, float, float]]:
    number_token = r"[0-9][0-9,]*(?:\.\d+)?"
    grade_pattern = "|".join(sorted(GRADE_CODES, key=len, reverse=True))
    pattern = re.compile(
        rf"^\s*({grade_pattern})\s+({number_token})\s+({number_token})\s+"
        rf"({number_token})\s+({number_token})\s+({number_token})\s+({number_token})\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    rows = [
        (match.group(1).upper(), *(_number(value) for value in match.groups()[1:]))
        for match in pattern.finditer(text)
    ]
    if not rows:
        raise NairobiCoffeeExchangeParseError("market total table has no recognized coffee grade rows")
    return rows


def parse_nairobi_coffee_exchange_market_report(
    document: str | bytes,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_days: float = 14.0,
) -> list[dict[str, Any]]:
    """Normalize the public auction market-total table by coffee grade.

    Prices in this source are explicitly USD per 50 kg.  They are observed
    auction averages, not executable bids or offers; every row is therefore
    synthetic-paper-only even when the report itself is fresh.
    """

    text = extract_pdf_text(document) if isinstance(document, bytes) else str(document or "")
    if not text.strip():
        raise NairobiCoffeeExchangeParseError("market report text is empty")
    normalized = text.replace("\u00a0", " ")
    lowered = normalized.casefold()
    required_markers = ("nairobi coffee exchange", "market total", "prices are in usd per 50 kg")
    if not all(marker in lowered for marker in required_markers):
        raise NairobiCoffeeExchangeParseError("market report identity, market-total, or USD-per-50kg marker was not found")
    sale_number, sale_date = _report_session(normalized)
    fetched_at = _received_at(received_at)
    age_seconds = max(0.0, (fetched_at.date() - sale_date).total_seconds())
    freshness_state = "fresh" if age_seconds <= max(0.0, float(stale_after_days)) * 86400.0 else "stale"
    session_status = "scheduled" if fetched_at.date() < sale_date else "completed"

    rows: list[dict[str, Any]] = []
    for grade, bags, weight_kg, minimum, maximum, value_usd, average in _grade_rows(normalized):
        if average <= 0 or minimum <= 0 or maximum <= 0 or minimum > maximum:
            raise NairobiCoffeeExchangeParseError(f"grade {grade} contains invalid price bounds")
        dispersion_usd = maximum - minimum
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:SALE:{sale_number}:GRADE:{grade}",
                "instrument_id": f"{VENUE}:SALE:{sale_number}:GRADE:{grade}",
                "symbol": f"NCE_{grade}",
                "name": f"Nairobi Coffee Exchange sale {sale_number} grade {grade}",
                "base": f"KENYA_GREEN_COFFEE_GRADE_{grade}",
                "quote": "USD_PER_50_KG",
                "market_type": "green_coffee_auction_market_total",
                "market_surface": MARKET_SURFACE,
                "asset_class": "green_coffee",
                "trade_type": "official_auction_grade_market_total",
                "direction": "watch_only",
                "last": average,
                "price_available": True,
                "price_basis": "published_grade_weighted_average_usd_per_50kg",
                "published_min_price_usd_per_50kg": minimum,
                "published_max_price_usd_per_50kg": maximum,
                "published_average_price_usd_per_50kg": average,
                "grade_price_dispersion_usd_per_50kg": round(dispersion_usd, 6),
                "grade_price_dispersion_pct_of_average": round(dispersion_usd / average, 6),
                "bags_offered": int(bags) if bags.is_integer() else bags,
                "weight_offered_kg": weight_kg,
                "published_value_usd": value_usd,
                "seller_concentration_available": False,
                "data_access_type": "public_no_key",
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_auction_grade_market_total",
                "freshness_state": freshness_state,
                "freshness_basis": "official_auction_sale_date",
                "freshness_age_seconds": round(age_seconds, 3),
                "session_status": session_status,
                "auction_sale_number": sale_number,
                "auction_sale_date": sale_date.isoformat(),
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Nairobi Coffee Exchange public market report",
                "source_url": source_url,
                "exchange_about_url": ABOUT_URL,
                "paper_route_status": "synthetic_research_only",
                "execution_route_status": "route_needed",
            }
        )
    return rows


# Short compatibility alias for callers referring to the source's report label.
parse_nairobi_coffee_market_report = parse_nairobi_coffee_exchange_market_report


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(
    result: dict[str, Any], source_url: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:MARKET_REPORT:HEALTH",
            "instrument_id": f"{VENUE}:MARKET_REPORT:HEALTH",
            "symbol": "MARKET_REPORT_HEALTH",
            "base": "KENYA_GREEN_COFFEE",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": (
                "public_nairobi_coffee_exchange_parser_failure"
                if parser_error
                else "public_nairobi_coffee_exchange_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {
        **((root.get("adapters") or {}).get(adapter_id) or {}),
        **(root.get(adapter_id) or {}),
    }


class NairobiCoffeeExchangeAdapter:
    info = AdapterInfo(
        adapter_id="nairobi_coffee_exchange",
        venue=VENUE,
        market_type="green_coffee_auction_market_total",
        source="Nairobi Coffee Exchange public auction market reports",
        capabilities=(
            "public_market_data",
            "auction_grade_market_totals",
            "grade_price_min_max_average",
            "grade_price_dispersion",
            "auction_volume",
            "source_health",
        ),
        aliases=(
            "nairobi coffee exchange",
            "nce",
            "kenya coffee auction",
            "kenya green coffee grades",
            "coffee grade aa ab",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.nairobi_coffee_exchange.NairobiCoffeeExchangeAdapter",
        quote_assets=("USD_PER_50_KG",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 14.0)))
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_bytes(source_url, timeout)
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                document: str | bytes = (
                    str(result["text"]) if "text" in result else bytes(result.get("content") or b"")
                )
                observations = parse_nairobi_coffee_exchange_market_report(
                    document,
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                source_status = "reachable"
                freshness_states = {str(row["freshness_state"]) for row in observations}
                session_states = {str(row["session_status"]) for row in observations}
                freshness_state = next(iter(freshness_states)) if len(freshness_states) == 1 else "mixed"
                session_state = next(iter(session_states)) if len(session_states) == 1 else "mixed"
            except (NairobiCoffeeExchangeParseError, TypeError, ValueError) as exc:
                message = f"Nairobi Coffee Exchange market report parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"
        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1297,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url, ABOUT_URL],
                "fetch_status": {"market_report": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_seller_level_lots_bid_history_and_order_routing_not_available",
                "paper_only": True,
            },
        )


register_adapter(NairobiCoffeeExchangeAdapter())
