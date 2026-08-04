"""Official Energy Commission ENEGEM programme observations.

The public ENEGEM page documents Malaysia's cross-border renewable-energy
marketplace and its export-capacity policy.  It does not publish an auction
clearing price or an executable quote, so normalized programme observations
remain watch-only and cannot become an order route.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://www.st.gov.my/energy-exchange-malaysia-enegem"
LICENSE_REGISTRY_URL = (
    "https://www.st.gov.my/ms/pihak-berkepentingan/elektrik/"
    "pemegang-lesen-berdaftar-dan-profesional"
)
VPPA_URL = "https://www.st.gov.my/what-vppa"
MARKET_SURFACE = "malaysia_enegem_cross_border_renewable_electricity"


class EnegemParseError(ValueError):
    """Raised when the reachable official ENEGEM page changes schema."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _visible_text(document: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - preserve upstream parser evidence.
        raise EnegemParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_time(value: str | None) -> dt.datetime:
    raw = value or utc_now()
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnegemParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _month(value: str, field: str) -> str:
    try:
        return dt.datetime.strptime(value, "%B %Y").date().replace(day=1).isoformat()
    except ValueError as exc:
        raise EnegemParseError(f"{field} month is invalid: {value!r}") from exc


def parse_enegem_programme(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the official ENEGEM/CBES RE programme facts from its page."""

    if not isinstance(document, str) or not document.strip():
        raise EnegemParseError("ENEGEM response is empty")
    text = _visible_text(document)
    normalized = re.sub(r"[\u2010-\u2015]", "-", text)
    if not re.search(r"Energy Exchange Malaysia\s*\(ENEGEM\)", normalized, re.IGNORECASE):
        raise EnegemParseError("Energy Exchange Malaysia (ENEGEM) marker was not found")
    if not re.search(r"Cross-Border Electricity Sales.*?Renewable Energy|CBES\s*RE", normalized, re.IGNORECASE):
        raise EnegemParseError("CBES RE programme marker was not found")

    capacity_match = re.search(
        r"capacity\s+of\s+up\s+to\s+([0-9][0-9,.]*)\s*MW",
        normalized,
        re.IGNORECASE,
    )
    if not capacity_match:
        raise EnegemParseError("documented CBES RE export capacity was not found")
    try:
        capacity_mw = float(capacity_match.group(1).replace(",", ""))
    except ValueError as exc:
        raise EnegemParseError("documented CBES RE export capacity is invalid") from exc
    if capacity_mw <= 0:
        raise EnegemParseError("documented CBES RE export capacity must be positive")

    approval_match = re.search(
        r"approved\s+by\s+the\s+Government\s+in\s+([A-Za-z]+\s+\d{4})",
        normalized,
        re.IGNORECASE,
    )
    guide_match = re.search(
        r"Guide\s+for\s+Cross-Border\s+Electricity\s+Sales\s*\(CBES\)\s*,?\s*"
        r"(Third)\s+Edition\s+released\s+in\s+([A-Za-z]+\s+\d{4})",
        normalized,
        re.IGNORECASE,
    )
    if not approval_match:
        raise EnegemParseError("CBES RE government approval month was not found")
    if not guide_match:
        raise EnegemParseError("CBES guide edition and release month were not found")
    if not re.search(r"operated\s+by\s+the\s+Single\s+Buyer", normalized, re.IGNORECASE):
        raise EnegemParseError("ENEGEM Single Buyer operator marker was not found")
    if not re.search(
        r"Malaysia\s*(?:(?:-|to)\s*)?Singapore\s+interconnection",
        normalized,
        re.IGNORECASE,
    ):
        raise EnegemParseError("Malaysia-Singapore interconnection marker was not found")
    if not re.search(r"one\s+year\s+supply\s+period", normalized, re.IGNORECASE):
        raise EnegemParseError("initial one-year supply period marker was not found")

    fetched_at = _received_time(received_at)
    approval_month = _month(approval_match.group(1).title(), "approval")
    guide_release_month = _month(guide_match.group(2).title(), "guide release")
    inst_id = "ST_ENEGEM:CBES_RE:MYS_SGP"
    return [
        {
            "venue": "ST_ENEGEM",
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": "CBES_RE_MYS_SGP",
            "name": "ENEGEM CBES RE Malaysia-Singapore programme",
            "base": "MY_RENEWABLE_ELECTRICITY",
            "quote": "N/A",
            "market_type": "cross_border_electricity_programme_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "renewable_electricity",
            "trade_type": "official_market_programme_reference",
            "direction": "watch_only",
            "last": 0.0,
            "export_capacity_limit_mw": capacity_mw,
            "programme": "Cross-Border Electricity Sales for Renewable Energy",
            "programme_acronym": "CBES RE",
            "platform": "Energy Exchange Malaysia",
            "platform_acronym": "ENEGEM",
            "operator": "Single Buyer",
            "regulator": "Suruhanjaya Tenaga (Energy Commission)",
            "origin_country": "Malaysia",
            "initial_destination_country": "Singapore",
            "interconnection": "Malaysia-Singapore interconnection",
            "approval_month": approval_month,
            "guide_edition": guide_match.group(1).title(),
            "guide_release_month": guide_release_month,
            "initial_supply_period_years": 1.0,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_programme_reference",
            "freshness_state": "fresh",
            "freshness_basis": "official_page_fetch_timestamp",
            "freshness_age_seconds": 0.0,
            "session_status": "official_programme_reference",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Suruhanjaya Tenaga official ENEGEM page",
            "source_url": source_url,
            "candidate_reject_reason": "official_programme_page_has_no_clearing_price",
        }
    ]


# Compatibility names for callers that identify the parser by its page/surface.
parse_enegem_page = parse_enegem_programme
parse_enegem = parse_enegem_programme


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
    observation = health_observation("ST_ENEGEM", source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_programme_parser_failure"
                if parser_error
                else "public_programme_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class SuruhanjayaTenagaEnergyCommissionAdapter:
    info = AdapterInfo(
        adapter_id="suruhanjaya_tenaga_energy_commission",
        venue="ST_ENEGEM",
        market_type="cross_border_renewable_electricity",
        source="Suruhanjaya Tenaga official ENEGEM public page",
        capabilities=(
            "public_market_data",
            "catalog",
            "cross_border_renewable_electricity",
            "export_capacity",
            "interconnection_identity",
            "programme_governance",
            "source_health",
        ),
        aliases=(
            "suruhanjaya tenaga",
            "energy commission malaysia",
            "energy exchange malaysia",
            "enegem",
            "cross-border electricity sales for renewable energy",
            "cbes re",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.suruhanjaya_tenaga_energy_commission."
            "SuruhanjayaTenagaEnergyCommissionAdapter"
        ),
        quote_assets=(),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_enegem_programme(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
                freshness_state = str(observations[0]["freshness_state"])
                session_state = str(observations[0]["session_status"])
            except (EnegemParseError, TypeError, ValueError) as exc:
                message = f"ENEGEM programme parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        source_urls = list(dict.fromkeys([source_url, SOURCE_URL, LICENSE_REGISTRY_URL, VPPA_URL]))
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 588,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": source_urls,
                "fetch_status": {"enegem": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_export_clearing_prices_and_auction_schedule",
                "paper_only": True,
            },
        )


register_adapter(SuruhanjayaTenagaEnergyCommissionAdapter())
