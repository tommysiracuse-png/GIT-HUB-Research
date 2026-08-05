"""Government of India TReDS mandate reference observations.

The PIB release is a public, no-key policy source for India\'s Trade
Receivables Discounting System (TReDS).  It identifies the five operating
platforms and the CPSE routing mandate, but it does not expose invoice-level
discount rates, tenors, or executable financing offers.  Consequently this
adapter reports the documented platform and policy evidence as watch-only
paper research observations; it never creates an execution route or price.
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


SOURCE_URL = "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2283195&lang=1&reg=1"
PROCUREMENT_MANUAL_URL = "https://doe.gov.in/files/circulars_document/Manual_Goods_2024.pdf"
MARKET_SURFACE = "india_treds_msme_invoice_discounting"
PLATFORMS = ("RXIL", "M1xchange", "Invoicemart", "C2treds", "DTX")


class IndiaTredsPibParseError(ValueError):
    """Raised when the reachable PIB release no longer has the required facts."""


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
        raise IndiaTredsPibParseError("PIB TReDS release is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain changed public-page evidence.
        raise IndiaTredsPibParseError(f"invalid PIB HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndiaTredsPibParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _published_date(text: str) -> str:
    match = re.search(r"Posted\s+On:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text, re.IGNORECASE)
    if not match:
        raise IndiaTredsPibParseError("PIB release posted date was not found")
    try:
        return dt.datetime.strptime(match.group(1).title(), "%d %b %Y").date().isoformat()
    except ValueError as exc:
        raise IndiaTredsPibParseError("PIB release posted date is invalid") from exc


def _notification_date(text: str) -> str:
    match = re.search(
        r"Notification(?:,\s*issued)?\s*(?:dated|on)?\s*(?:on\s*)?"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise IndiaTredsPibParseError("TReDS notification date was not found")
    try:
        return dt.datetime.strptime(match.group(1).title(), "%d %B %Y").date().isoformat()
    except ValueError as exc:
        raise IndiaTredsPibParseError("TReDS notification date is invalid") from exc


def _volume_trend(text: str) -> tuple[float, str, float, str]:
    match = re.search(
        r"invoice\s+discounting\s+increasing\s+from\s*[₹Rs.\s]*([0-9][0-9,]*)\s*crore\s*"
        r"in\s*(FY\s*\d{4}-\d{2})\s*to\s*[₹Rs.\s]*([0-9][0-9,.]*)\s*lakh\s*crore\s*"
        r"in\s*(FY\s*\d{4}-\d{2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise IndiaTredsPibParseError("TReDS invoice-discounting growth figures were not found")
    try:
        start_crore = float(match.group(1).replace(",", ""))
        end_lakh_crore = float(match.group(3).replace(",", ""))
    except ValueError as exc:
        raise IndiaTredsPibParseError("TReDS invoice-discounting growth figures are invalid") from exc
    if start_crore <= 0 or end_lakh_crore <= 0:
        raise IndiaTredsPibParseError("TReDS invoice-discounting growth figures must be positive")
    return start_crore, re.sub(r"\s+", " ", match.group(2).upper()), end_lakh_crore * 100_000.0, re.sub(
        r"\s+", " ", match.group(4).upper()
    )


def _platform_symbol(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def parse_india_treds_pib_release(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the official PIB TReDS mandate into five platform observations."""

    text = _visible_text(document)
    normalized = re.sub(r"[\u2010-\u2015]", "-", text)
    if not re.search(r"Ministry\s+of\s+Micro\s*,?\s*Small\s*&?\s*Medium\s+Enterprises", normalized, re.I):
        raise IndiaTredsPibParseError("Ministry of MSME marker was not found")
    if not re.search(r"Trade\s+Receivables\s+Discounting\s+System\s*\(\s*TReDS\s*\)", normalized, re.I):
        raise IndiaTredsPibParseError("Trade Receivables Discounting System marker was not found")
    if not re.search(r"(?:all\s+operating\s+)?Central\s+Public\s+Sector\s+Enterprises\s*\(\s*CPSEs\s*\)", normalized, re.I):
        raise IndiaTredsPibParseError("CPSE routing mandate marker was not found")
    if not re.search(r"mandatory\s+(?:use|settlement).*?TReDS", normalized, re.I):
        raise IndiaTredsPibParseError("mandatory TReDS settlement marker was not found")
    if not re.search(r"RBI-regulated\s+electronic\s+platform", normalized, re.I):
        raise IndiaTredsPibParseError("RBI-regulated platform marker was not found")
    if not re.search(r"competitive\s+bidding\s+by\s+multiple\s+financiers", normalized, re.I):
        raise IndiaTredsPibParseError("competitive financier-bidding marker was not found")

    platform_match = re.search(
        r"Five\s+platforms\s+(?:are\s+)?currently\s+operational\s*:\s*"
        r"RXIL\s*,\s*M1xchange\s*,\s*Invoicemart\s*,\s*C2treds\s*(?:,?\s*and\s*|\s+and\s+)DTX",
        normalized,
        re.IGNORECASE,
    )
    if not platform_match:
        raise IndiaTredsPibParseError("the five operational TReDS platforms were not found")

    released_on = _published_date(normalized)
    notification_on = _notification_date(normalized)
    start_crore, start_fy, end_crore, end_fy = _volume_trend(normalized)
    fetched_at = _received_at(received_at)
    return [
        {
            "venue": "INDIA_TREDS",
            "inst_id": f"INDIA_TREDS:{_platform_symbol(platform)}",
            "instrument_id": f"INDIA_TREDS:{_platform_symbol(platform)}",
            "symbol": _platform_symbol(platform),
            "name": f"India TReDS platform: {platform}",
            "base": "MSME_TRADE_RECEIVABLE",
            "quote": "INR",
            "market_type": "invoice_discounting_platform_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "trade_receivable",
            "trade_type": "official_invoice_finance_platform_reference",
            "direction": "watch_only",
            "last": 0.0,
            "price_basis": "policy_reference_only_no_invoice_discount_rate",
            "platform": platform,
            "platform_count": len(PLATFORMS),
            "regulator": "Reserve Bank of India",
            "operator_jurisdiction": "India",
            "eligible_supplier": "MSME",
            "buyer_types": ["corporate buyers", "Government Departments", "Public Sector Undertakings"],
            "cpse_mandatory_treds_routing": True,
            "financing_basis": "competitive_bidding_by_multiple_financiers",
            "financing_characteristics": ["collateral_free", "without_recourse_to_seller"],
            "notification_date": notification_on,
            "release_date": released_on,
            "invoice_discounting_start_crore_inr": start_crore,
            "invoice_discounting_start_fiscal_year": start_fy,
            "invoice_discounting_end_crore_inr": end_crore,
            "invoice_discounting_end_fiscal_year": end_fy,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_policy_platform_reference",
            "freshness_state": "fresh",
            "freshness_basis": "official_pib_release_fetch_timestamp",
            "freshness_age_seconds": 0.0,
            "session_status": "official_policy_reference",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Government of India Ministry of MSME PIB release",
            "source_url": source_url,
            "candidate_reject_reason": "public_treds_release_has_no_invoice_level_discount_rate",
        }
        for platform in PLATFORMS
    ]


# Compatibility names for callers that identify the source by government or policy surface.
parse_government_of_india_msme_pib = parse_india_treds_pib_release
parse_pib_treds_release = parse_india_treds_pib_release


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
    row = health_observation("INDIA_TREDS", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_treds_policy_parser_failure"
                if parser_error
                else "public_treds_policy_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class GovernmentOfIndiaMinistryOfMsmePibAdapter:
    info = AdapterInfo(
        adapter_id="government_of_india_ministry_of_msme_pib",
        venue="INDIA_TREDS",
        market_type="invoice_discounting_platform_reference",
        source="Government of India Ministry of MSME PIB TReDS release",
        capabilities=(
            "public_market_data",
            "invoice_discounting_platform_catalog",
            "msme_receivables",
            "cpse_procurement_routing",
            "policy_reference",
            "source_health",
        ),
        aliases=(
            "government of india",
            "ministry of msme",
            "press information bureau",
            "pib",
            "treds",
            "trade receivables discounting system",
            "rxil",
            "m1xchange",
            "invoicemart",
            "c2treds",
            "dtx",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.government_of_india_ministry_of_msme_pib."
            "GovernmentOfIndiaMinistryOfMsmePibAdapter"
        ),
        quote_assets=("INR",),
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
                observations = parse_india_treds_pib_release(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
                freshness_state = "fresh"
                session_state = "official_policy_reference"
            except (IndiaTredsPibParseError, TypeError, ValueError) as exc:
                message = f"India TReDS PIB parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 997,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url],
                "reference_urls": list(dict.fromkeys([source_url, SOURCE_URL, PROCUREMENT_MANUAL_URL])),
                "fetch_status": {"pib_treds_release": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_invoice_level_discount_rates_tenors_and_executable_financing_offers",
                "paper_only": True,
            },
        )


register_adapter(GovernmentOfIndiaMinistryOfMsmePibAdapter())
