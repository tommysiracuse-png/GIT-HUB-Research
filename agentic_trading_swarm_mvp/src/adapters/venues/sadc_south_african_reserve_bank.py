"""SADC-RTGS multicurrency settlement reference adapter.

The SADC announcement and SARB regional-settlement page are public policy and
system-reference sources, not executable FX venues.  The observations below
therefore remain watch-only even when a source is healthy: they describe the
AOA settlement-currency onboarding and the regional participant roster only.
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


ANNOUNCEMENT_URL = "https://www.sadc.int/latest-news/angolan-kwanza-introduced-sadc-rtgs-system"
SARB_RTGS_URL = "https://www.sarb.co.za/en/home/what-we-do/payments-and-settlements/SADC-RTGS"
MARKET_SURFACE = "sadc_rtgs_multicurrency_settlement"
VENUE = "SADC_RTGS"


class SadcRtgsParseError(ValueError):
    """Raised when an official SADC-RTGS public page no longer has expected facts."""


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
    if not isinstance(document, str) or not document.strip():
        raise SadcRtgsParseError("official response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - upstream parser failures are health evidence.
        raise SadcRtgsParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SadcRtgsParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _normalize(text: str) -> str:
    return re.sub(r"[\u2010-\u2015]", "-", text)


def _announcement_date(text: str) -> str:
    match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b", text)
    if not match:
        raise SadcRtgsParseError("announcement publication date was not found")
    try:
        return dt.datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError as exc:
        raise SadcRtgsParseError("announcement publication date is invalid") from exc


def parse_kwanza_onboarding(
    document: str,
    *,
    source_url: str = ANNOUNCEMENT_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the official AOA SADC-RTGS settlement-currency onboarding notice."""

    text = _normalize(_visible_text(document))
    if not re.search(r"Angolan\s+Kwanza\s+introduced\s+into\s+the\s+SADC-RTGS", text, re.I):
        raise SadcRtgsParseError("Angolan kwanza SADC-RTGS announcement marker was not found")
    if not re.search(r"kwanza\s+as\s+a\s+settlement\s+currency", text, re.I):
        raise SadcRtgsParseError("AOA settlement-currency onboarding marker was not found")
    if not re.search(r"second\s+settlement\s+currency", text, re.I):
        raise SadcRtgsParseError("second settlement-currency marker was not found")
    if not re.search(r"exclusively\s+in\s+South\s+African\s+rand", text, re.I):
        raise SadcRtgsParseError("prior ZAR-only settlement marker was not found")

    participant_match = re.search(r"currently\s+(\d+)\s+countries\s+participating", text, re.I)
    if not participant_match:
        raise SadcRtgsParseError("participating-country count was not found")
    participant_country_count = int(participant_match.group(1))
    if participant_country_count <= 0:
        raise SadcRtgsParseError("participating-country count must be positive")

    event_match = re.search(r"on\s+(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})\s+formally announced", text, re.I)
    if not event_match:
        raise SadcRtgsParseError("formal onboarding announcement date was not found")
    try:
        onboarding_date = dt.datetime.strptime(event_match.group(1), "%d %B %Y").date().isoformat()
    except ValueError as exc:
        raise SadcRtgsParseError("formal onboarding announcement date is invalid") from exc

    settled_since_match = re.search(r"since\s+its\s+inception\s+in\s+(\d{4})", text, re.I)
    if not settled_since_match:
        raise SadcRtgsParseError("SADC-RTGS inception year was not found")
    fetched_at = _received_time(received_at)
    publication_date = _announcement_date(text)
    inst_id = f"{VENUE}:SETTLEMENT_CURRENCY:AOA"
    return [
        {
            "venue": VENUE,
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": "AOA_SETTLEMENT_ONBOARDING",
            "name": "Angolan kwanza SADC-RTGS settlement-currency onboarding",
            "base": "AOA",
            "quote": "SADC_RTGS",
            "market_type": "cross_border_payment_settlement_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "settlement_currency",
            "trade_type": "official_payment_system_reference",
            "direction": "watch_only",
            "last": 0.0,
            "settlement_currency": "AOA",
            "settlement_currency_name": "Angolan kwanza",
            "settlement_currency_count": 2,
            "prior_settlement_currency": "ZAR",
            "prior_settlement_currency_name": "South African rand",
            "onboarding_date": onboarding_date,
            "announcement_date": publication_date,
            "system_inception_year": int(settled_since_match.group(1)),
            "participating_country_count_at_announcement": participant_country_count,
            "operator": "South African Reserve Bank",
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_settlement_currency_onboarding",
            "freshness_state": "fresh",
            "freshness_basis": "official_page_fetch_timestamp",
            "freshness_age_seconds": 0.0,
            "session_status": "multicurrency_settlement_enabled",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "SADC official announcement",
            "source_url": source_url,
            "candidate_reject_reason": "official_settlement_reference_has_no_executable_fx_quote",
        }
    ]


def parse_participant_roster(
    document: str,
    *,
    source_url: str = SARB_RTGS_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize SARB's public SADC-RTGS member-country roster."""

    text = _normalize(_visible_text(document))
    if not re.search(r"SADC-RTGS", text, re.I):
        raise SadcRtgsParseError("SADC-RTGS marker was not found")
    if not re.search(r"operated\s+by\s+the\s+South\s+African\s+Reserve\s+Bank", text, re.I):
        raise SadcRtgsParseError("SARB operator marker was not found")
    roster_match = re.search(
        r"Membership\s+comprises\s+(\d+)\s+countries\s+namely,\s*(.+?)(?=\s+In\s+1992\b|\s+The\s+SADC-RTGS\s+payment\s+platform\b|$)",
        text,
        re.I,
    )
    if not roster_match:
        raise SadcRtgsParseError("SADC-RTGS membership roster was not found")
    declared_count = int(roster_match.group(1))
    countries = tuple(
        part.strip(" .")
        for part in re.split(r",\s*|\s+and\s+", roster_match.group(2).strip())
        if part.strip(" .")
    )
    if declared_count <= 0 or len(countries) != declared_count:
        raise SadcRtgsParseError(
            f"membership roster count mismatch: declared {declared_count}, parsed {len(countries)}"
        )

    fetched_at = _received_time(received_at)
    inst_id = f"{VENUE}:PARTICIPANT_ROSTER"
    return [
        {
            "venue": VENUE,
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": "SADC_RTGS_PARTICIPANT_ROSTER",
            "name": "SADC-RTGS participating member-country roster",
            "base": "SADC_RTGS",
            "quote": "N/A",
            "market_type": "cross_border_payment_settlement_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "payment_system_participation",
            "trade_type": "official_payment_system_reference",
            "direction": "watch_only",
            "last": 0.0,
            "member_country_count": declared_count,
            "participant_country_count": declared_count,
            "member_countries": countries,
            "operator": "South African Reserve Bank",
            "system_type": "regional cross-border real-time gross settlement",
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_participant_roster",
            "freshness_state": "fresh",
            "freshness_basis": "official_page_fetch_timestamp",
            "freshness_age_seconds": 0.0,
            "session_status": "regional_settlement_system_reference",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "South African Reserve Bank SADC-RTGS page",
            "source_url": source_url,
            "candidate_reject_reason": "official_participant_roster_has_no_executable_fx_quote",
        }
    ]


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
    result: dict[str, Any], source_url: str, source_key: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}_HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}_HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_settlement_reference_parser_failure"
                if parser_error
                else "public_settlement_reference_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class SadcSouthAfricanReserveBankAdapter:
    info = AdapterInfo(
        adapter_id="sadc_south_african_reserve_bank",
        venue=VENUE,
        market_type="cross_border_payment_settlement_reference",
        source="SADC and South African Reserve Bank public SADC-RTGS references",
        capabilities=(
            "public_market_data",
            "settlement_reference",
            "settlement_currency_onboarding",
            "participant_roster",
            "cross_border_payments",
            "source_health",
        ),
        aliases=(
            "sadc rtgs",
            "sadc-rtgs",
            "siress",
            "south african reserve bank",
            "angolan kwanza",
            "aoa settlement currency",
        ),
        docs_url=ANNOUNCEMENT_URL,
        runtime_entrypoint=(
            "adapters.venues.sadc_south_african_reserve_bank.SadcSouthAfricanReserveBankAdapter"
        ),
        quote_assets=("AOA", "ZAR"),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        announcement_url = str(cfg.get("announcement_source_url") or ANNOUNCEMENT_URL)
        roster_url = str(cfg.get("roster_source_url") or SARB_RTGS_URL)
        sources = (
            ("announcement", announcement_url, parse_kwanza_onboarding),
            ("roster", roster_url, parse_participant_roster),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        successful_sources = 0
        unavailable_statuses: list[str] = []

        for source_key, source_url, parser in sources:
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                unavailable_statuses.append(str(result.get("status") or "unavailable"))
                observations.append(_failure_observation(result, source_url, source_key))
                continue
            try:
                rows = parser(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                observations.extend(rows)
                successful_sources += 1
            except (SadcRtgsParseError, TypeError, ValueError) as exc:
                message = f"{source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations.append(_failure_observation(result, source_url, source_key, message))

        if parser_failures:
            source_status = "degraded"
        elif successful_sources == len(sources):
            source_status = "reachable"
        elif successful_sources:
            source_status = "partial"
        elif unavailable_statuses and len(set(unavailable_statuses)) == 1:
            source_status = unavailable_statuses[0]
        else:
            source_status = "unavailable"
        real_observation_count = sum(1 for row in observations if not row.get("parser_failure") and row.get("last") == 0.0 and row.get("quality_status"))
        freshness_state = "fresh" if successful_sources else "unknown"
        session_state = "mixed" if successful_sources == len(sources) else "unknown"
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 462,
                "source_status": source_status,
                "source_urls": [announcement_url, roster_url],
                "fetch_status": fetch_status,
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": real_observation_count,
                "capability_gap": "public_corridor_prices_spreads_and_payment_costs",
                "paper_only": True,
            },
        )


register_adapter(SadcSouthAfricanReserveBankAdapter())
