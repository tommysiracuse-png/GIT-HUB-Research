"""Enagás GTS public renewable-gas guarantees-of-origin references.

Enagás GTS publishes the Spanish renewable-gas guarantees-of-origin (GdO)
system's public description.  The public page documents registry operations,
but not executable certificate bids, offers, or clearing prices.  This plugin
therefore records the documented certificate classes as paper-only reference
observations and never creates an execution route.
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


SOURCE_URL = "https://www.enagas.es/en/technical-management-system/general-information/guarantees-origin/"
SYSTEM_PORTAL_URL = "https://www.gdogas.es/en/public-portal/home"
NEWS_URL = "https://www.enagas.es/en/press-room/news-room/news/system-guarantees-origin-renewable-gases/"
ANNUAL_REPORT_URL = (
    "https://www.enagas.es/content/dam/enagas/en/files/accionistas-e-inversores/"
    "informacion-economico-financiera/cuentas-anuales-auditadas-e-informe-de-auditoria/"
    "2020/ccaa-consolidadas-2024-en.pdf"
)
AIB_DATASHEET_URL = (
    "https://www.aib-net.org/sites/default/files/assets/facts/national-datasheets/"
    "REGADISS/2024_ENAGAS%20GTS%20Spain%20-%20AIB%20Data%20Sheet%20on%20disclosure_v2_.pdf"
)
VENUE = "ENAGAS_GTS"
MARKET_SURFACE = "spain_renewable_gas_guarantees_of_origin"


class EnagasGtsParseError(ValueError):
    """Raised when the official GdO page no longer identifies the registry."""


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
    except Exception as exc:  # noqa: BLE001 - retain upstream parser evidence.
        raise EnagasGtsParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnagasGtsParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


_CERTIFICATE_CLASSES = (
    {
        "code": "BIOMETHANE_GRID",
        "symbol": "GDO_BIOMETHANE_GRID",
        "name": "Spain biomethane grid-injection guarantee of origin",
        "gas_type": "biomethane",
        "logistics_class": "gas_system_injection",
        "transferability": "registry_transfer_subject_to_system_rules",
    },
    {
        "code": "BIOGAS_SELF_CONSUMPTION",
        "symbol": "GDO_BIOGAS_SELF_CONSUMPTION",
        "name": "Spain biogas self-consumption guarantee of origin",
        "gas_type": "biogas",
        "logistics_class": "self_consumption",
        "transferability": "self_cancelled_not_transferable",
    },
    {
        "code": "RENEWABLE_HYDROGEN_OFF_GRID",
        "symbol": "GDO_RENEWABLE_HYDROGEN_OFF_GRID",
        "name": "Spain off-grid renewable hydrogen guarantee of origin",
        "gas_type": "renewable_hydrogen",
        "logistics_class": "off_grid",
        "transferability": "registry_transfer_subject_to_system_rules",
    },
    {
        "code": "BIO_LNG_OFF_GRID",
        "symbol": "GDO_BIO_LNG_OFF_GRID",
        "name": "Spain bio-LNG guarantee of origin",
        "gas_type": "bio_lng",
        "logistics_class": "off_grid",
        "transferability": "registry_transfer_subject_to_system_rules",
    },
)


def parse_enagas_gts_guarantees_of_origin(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the official Spanish renewable-gas GdO registry surface."""

    if not isinstance(document, str) or not document.strip():
        raise EnagasGtsParseError("guarantees-of-origin response is empty")
    text = _visible_text(document)
    normalized = re.sub(r"[\u2010-\u2015]", "-", text)
    if not re.search(r"guarantees?\s+of\s+origin", normalized, re.IGNORECASE):
        raise EnagasGtsParseError("guarantees-of-origin marker was not found")
    if not re.search(r"renewable\s+gases?", normalized, re.IGNORECASE):
        raise EnagasGtsParseError("renewable-gases marker was not found")
    if not re.search(r"issuance.*transfer.*(?:import.*export|export.*import).*cancellation", normalized, re.IGNORECASE):
        raise EnagasGtsParseError("registry operation markers were not found")
    if not re.search(r"(?:Enag[aá]s\s+)?GTS|Technical\s+Manager\s+of\s+the\s+System", normalized, re.IGNORECASE):
        raise EnagasGtsParseError("Enagás GTS responsibility marker was not found")
    if not re.search(r"(?:1\s*MWh|one\s*MWh)", normalized, re.IGNORECASE):
        raise EnagasGtsParseError("one-MWh certificate-unit marker was not found")

    fetched_at = _received_time(received_at)
    observations: list[dict[str, Any]] = []
    for certificate in _CERTIFICATE_CLASSES:
        inst_id = f"{VENUE}:GDO:{certificate['code']}"
        observations.append(
            {
                "venue": VENUE,
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": certificate["symbol"],
                "name": certificate["name"],
                "base": certificate["gas_type"].upper(),
                "quote": "GDO_MWH",
                "market_type": "renewable_gas_guarantee_of_origin_reference",
                "market_surface": MARKET_SURFACE,
                "asset_class": "renewable_gas_certificate",
                "trade_type": "official_registry_certificate_reference",
                "direction": "watch_only",
                "last": 0.0,
                "certificate_unit_mwh": 1.0,
                "gas_type": certificate["gas_type"],
                "logistics_class": certificate["logistics_class"],
                "transferability": certificate["transferability"],
                "registry_operations": ["issuance", "transfer", "import", "export", "cancellation"],
                "jurisdiction": "Spain",
                "registry_operator": "Enagás GTS",
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_registry_reference",
                "freshness_state": "fresh",
                "freshness_basis": "official_registry_page_fetch_timestamp",
                "freshness_age_seconds": 0.0,
                "session_status": "registry_reference",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Enagás GTS guarantees-of-origin public page",
                "source_url": source_url,
                "supporting_source_urls": [SYSTEM_PORTAL_URL, NEWS_URL, ANNUAL_REPORT_URL, AIB_DATASHEET_URL],
                "candidate_reject_reason": "official_registry_page_has_no_public_clearing_price",
            }
        )
    return observations


parse_enagas_gts = parse_enagas_gts_guarantees_of_origin
parse_enagas_gts_go = parse_enagas_gts_guarantees_of_origin


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
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "market_type": "renewable_gas_guarantee_of_origin_reference",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_renewable_gas_go_parser_failure"
                if parser_error
                else "public_renewable_gas_go_source_unavailable"
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


class EnagasGtsRenewableGasGuaranteesOfOriginAdapter:
    info = AdapterInfo(
        adapter_id="enagas_gts_renewable_gas_guarantees_of_origin",
        venue=VENUE,
        market_type="renewable_gas_guarantee_of_origin_reference",
        source="Enagás GTS renewable-gas guarantees-of-origin public registry",
        capabilities=(
            "public_market_data",
            "renewable_gas_guarantees_of_origin",
            "biomethane",
            "biogas_self_consumption",
            "off_grid_hydrogen",
            "bio_lng",
            "registry_operations",
            "source_health",
        ),
        aliases=(
            "enagas gts",
            "enagás gts",
            "spain renewable gas guarantees of origin",
            "gdogas",
            "gdo gas",
            "spanish renewable gas go",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.enagas_gts."
            "EnagasGtsRenewableGasGuaranteesOfOriginAdapter"
        ),
        quote_assets=("GDO_MWH",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, max(1, int(cfg.get("timeout_seconds", 15))))
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_enagas_gts_guarantees_of_origin(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
            except (EnagasGtsParseError, TypeError, ValueError) as exc:
                message = f"Enagás GTS guarantees-of-origin parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"

        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1498,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url, SOURCE_URL, SYSTEM_PORTAL_URL, NEWS_URL, ANNUAL_REPORT_URL, AIB_DATASHEET_URL],
                "fetch_status": {"guarantees_of_origin": _fetch_evidence(result, source_url)},
                "freshness_state": "fresh" if "fresh" in freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_certificate_prices_order_book_and_transfer_volume_by_class",
                "paper_only": True,
            },
        )


register_adapter(EnagasGtsRenewableGasGuaranteesOfOriginAdapter())
