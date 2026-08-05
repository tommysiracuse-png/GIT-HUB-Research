"""Public HNX/SSC Vietnam carbon-allowance calendar adapter.

HNX's public carbon pages publish allocation and trading-calendar facts for
Vietnam's domestic allowance market, not a tradable price feed.  The records
are intentionally watch-only, including while the exchange is between its
announced first and last trading dates.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


CARBON_PORTAL_URL = "https://www.hnx.vn/en-gb/cacbon.html"
TRADING_NOTICE_URL = (
    "https://www.hnx.vn/vi-vn/m-tin-tuc-hnx/"
    "Notice%20on%20first%20and%20last%20trading%20dates%20of%20carbon%20"
    "emission%20allowance%20allocated%20in%2020252026%20period-60022952-0.html"
)
SSC_PORTAL_URL = "https://ssc.gov.vn/webcenter/portal/ssc"
SSC_DECREE_URL = (
    "https://ssc.gov.vn/cs/idcplg?IdcService=GET_FILE&allowInterrupt=1&"
    "dDocName=APPSSCGOVVN1620166587&dID=175298&filename=29_2026_ND_EN.pdf"
)
SOURCE_URL = CARBON_PORTAL_URL
MARKET_SURFACE = "vietnam_domestic_carbon_exchange_vn2025"
VIETNAM_TIME = dt.timezone(dt.timedelta(hours=7))


class HanoiSscCarbonParseError(ValueError):
    """Raised when a reachable HNX/SSC public document has changed shape."""


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
        raise HanoiSscCarbonParseError("official HNX page response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - preserve source parser evidence.
        raise HanoiSscCarbonParseError(f"invalid official HNX HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).replace("\xa0", " ").split())


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HanoiSscCarbonParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date_from_text(text: str, label: str) -> dt.date:
    patterns = (
        rf"{label}.{{0,100}}?(\d{{1,2}})\s*[/-]\s*(\d{{1,2}})\s*[/-]\s*(20\d{{2}})",
        rf"{label}.{{0,100}}?(20\d{{2}})\s*[/-]\s*(\d{{1,2}})\s*[/-]\s*(\d{{1,2}})",
        rf"{label}.{{0,100}}?([A-Z][a-z]+)\s+(\d{{1,2}}),?\s*(20\d{{2}})",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            if index == 0:
                day, month, year = (int(value) for value in match.groups())
            elif index == 1:
                year, month, day = (int(value) for value in match.groups())
            else:
                month = dt.datetime.strptime(match.group(1).title(), "%B").month
                day, year = int(match.group(2)), int(match.group(3))
            return dt.date(year, month, day)
        except ValueError:
            continue
    raise HanoiSscCarbonParseError(f"official notice is missing {label} date")


def _allocation_volume(text: str) -> int:
    match = re.search(
        r"(511[,.\s]?473[,.\s]?846|[0-9][0-9,.\s]{6,})\s*"
        r"(?:tCO2e|ton(?:ne)?s?\s*(?:of\s*)?(?:CO2e|carbon dioxide equivalent))",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise HanoiSscCarbonParseError("official notice is missing allocated allowance volume")
    digits = re.sub(r"\D", "", match.group(1))
    if digits != "511473846":
        raise HanoiSscCarbonParseError("official notice allocation volume does not match VN2025")
    return int(digits)


def _freshness(event_date: dt.date, fetched_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    local_date = fetched_at.astimezone(VIETNAM_TIME).date()
    age_seconds = max(0.0, float((local_date - event_date).days * 86400))
    return (
        "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400.0 else "stale",
        round(age_seconds, 3),
    )


def _session_status(first_trade: dt.date, last_trade: dt.date, fetched_at: dt.datetime) -> str:
    today = fetched_at.astimezone(VIETNAM_TIME).date()
    if today < first_trade:
        return "scheduled"
    if today > last_trade:
        return "closed"
    return "open"


def parse_hnx_vn2025_trading_notice(
    document: str,
    *,
    source_url: str = TRADING_NOTICE_URL,
    received_at: str | None = None,
    stale_after_days: float = 730.0,
) -> list[dict[str, Any]]:
    """Normalize HNX's VN2025 allowance allocation and trading calendar."""

    text = _visible_text(document)
    required = {
        "VN2025 allowance code": r"\bVN\s*2025\b",
        "carbon-emission allowance": r"carbon\s+emission\s+allowance|greenhouse\s+gas\s+emission\s+allowance",
        "2025-2026 compliance period": r"2025\s*[-/]\s*2026",
        "first trading date": r"first\s+trading\s+date",
        "last trading date": r"last\s+trading\s+date",
    }
    missing = [label for label, pattern in required.items() if not re.search(pattern, text, re.IGNORECASE)]
    if missing:
        raise HanoiSscCarbonParseError("official notice is missing required markers: " + ", ".join(missing))
    first_trade = _date_from_text(text, "first\\s+trading\\s+date")
    last_trade = _date_from_text(text, "last\\s+trading\\s+date")
    if last_trade < first_trade:
        raise HanoiSscCarbonParseError("official notice last trading date precedes first trading date")
    allocated_volume = _allocation_volume(text)
    fetched_at = _received_at(received_at)
    freshness_state, freshness_age = _freshness(first_trade, fetched_at, stale_after_days)
    return [
        {
            "venue": "HNX_SSC",
            "inst_id": "HNX_SSC:VN2025:ALLOWANCE:2025_2026",
            "instrument_id": "HNX_SSC:VN2025:ALLOWANCE:2025_2026",
            "symbol": "VN2025",
            "name": "Vietnam greenhouse gas emission allowance VN2025",
            "base": "VN2025_ALLOWANCE",
            "quote": "VND_PER_TCO2E",
            "market_type": "emission_allowance_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "greenhouse_gas_emission_allowance",
            "trade_type": "official_trading_calendar_reference",
            "direction": "watch_only",
            "last": 0.0,
            "allocation_volume_tco2e": allocated_volume,
            "compliance_period": "2025-2026",
            "first_trading_date": first_trade.isoformat(),
            "last_trading_date": last_trade.isoformat(),
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_allowance_allocation_and_trading_calendar",
            "freshness_state": freshness_state,
            "freshness_basis": "official_first_trading_date",
            "freshness_age_seconds": freshness_age,
            "session_status": _session_status(first_trade, last_trade, fetched_at),
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "No public entry-quality VN2025 price was published in the HNX notice",
            "source_url": source_url,
            "candidate_reject_reason": "official_allowance_calendar_not_executable_quote",
        }
    ]


def parse_hnx_carbon_portal(
    document: str,
    *,
    source_url: str = CARBON_PORTAL_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the reachable HNX carbon-market portal as a live source observation."""

    text = _visible_text(document)
    if not re.search(r"carbon", text, re.IGNORECASE) or not re.search(r"Hanoi Stock Exchange|\bHNX\b", text, re.IGNORECASE):
        raise HanoiSscCarbonParseError("official carbon portal is missing HNX carbon-market markers")
    fetched_at = _received_at(received_at)
    return [
        {
            "venue": "HNX_SSC",
            "inst_id": "HNX_SSC:CARBON_MARKET:PORTAL",
            "instrument_id": "HNX_SSC:CARBON_MARKET:PORTAL",
            "symbol": "HNX_CARBON_PORTAL",
            "name": "Hanoi Stock Exchange domestic carbon market portal",
            "base": "VN_CARBON_MARKET",
            "quote": "VND",
            "market_type": "carbon_market_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "carbon_market_operations",
            "trade_type": "official_market_portal_reference",
            "direction": "watch_only",
            "last": 0.0,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_carbon_market_portal",
            "freshness_state": "fresh",
            "freshness_basis": "official_portal_fetch",
            "freshness_age_seconds": 0.0,
            "session_status": "reference_only",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Hanoi Stock Exchange public carbon-market portal",
            "source_url": source_url,
            "candidate_reject_reason": "official_market_portal_not_executable_quote",
        }
    ]


def parse_ssc_decree_publication(
    content: bytes,
    *,
    source_url: str = SSC_DECREE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Record SSC's public decree PDF without claiming it is a price feed."""

    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise HanoiSscCarbonParseError("SSC decree endpoint did not return a PDF document")
    fetched_at = _received_at(received_at)
    return [
        {
            "venue": "HNX_SSC",
            "inst_id": "HNX_SSC:DECREE_29_2026:REGULATORY_REFERENCE",
            "instrument_id": "HNX_SSC:DECREE_29_2026:REGULATORY_REFERENCE",
            "symbol": "DECREE_29_2026",
            "name": "Vietnam Decree 29/2026 regulatory reference",
            "base": "VN_CARBON_MARKET",
            "quote": "N/A",
            "market_type": "carbon_market_regulatory_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "carbon_market_rule",
            "trade_type": "official_regulatory_reference",
            "direction": "watch_only",
            "last": 0.0,
            "regulation": "Decree 29/2026/ND-CP",
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_regulatory_document",
            "freshness_state": "fresh",
            "freshness_basis": "official_pdf_fetch",
            "freshness_age_seconds": 0.0,
            "session_status": "reference_only",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "State Securities Commission public regulatory document",
            "source_url": source_url,
            "candidate_reject_reason": "official_regulatory_document_not_executable_quote",
        }
    ]


def parse_ssc_portal(
    document: str,
    *,
    source_url: str = SSC_PORTAL_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the public SSC portal as regulator-source health evidence."""

    text = _visible_text(document)
    if not re.search(r"State\s+Securities\s+Commission|\bSSC\b", text, re.IGNORECASE):
        raise HanoiSscCarbonParseError("official SSC portal is missing regulator markers")
    fetched_at = _received_at(received_at)
    return [
        {
            "venue": "HNX_SSC",
            "inst_id": "HNX_SSC:SSC:REGULATOR_PORTAL",
            "instrument_id": "HNX_SSC:SSC:REGULATOR_PORTAL",
            "symbol": "SSC_PORTAL",
            "name": "Vietnam State Securities Commission public portal",
            "base": "VN_CARBON_MARKET",
            "quote": "N/A",
            "market_type": "carbon_market_regulatory_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "market_regulator_reference",
            "trade_type": "official_regulator_portal_reference",
            "direction": "watch_only",
            "last": 0.0,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_regulator_portal",
            "freshness_state": "fresh",
            "freshness_basis": "official_portal_fetch",
            "freshness_age_seconds": 0.0,
            "session_status": "reference_only",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "State Securities Commission public portal",
            "source_url": source_url,
            "candidate_reject_reason": "official_regulator_portal_not_executable_quote",
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
    result: dict[str, Any], source_url: str, label: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("HNX_SSC", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"HNX_SSC:ADAPTER_HEALTH:{label.upper()}",
            "instrument_id": f"HNX_SSC:ADAPTER_HEALTH:{label.upper()}",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_vn2025_allowance_parser_failure"
                if parser_error
                else "public_vn2025_allowance_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class HanoiStockExchangeStateSecuritiesCommissionAdapter:
    info = AdapterInfo(
        adapter_id="hanoi_stock_exchange_state_securities_commission",
        venue="HNX_SSC",
        market_type="emission_allowance_reference",
        source="Hanoi Stock Exchange and State Securities Commission public Vietnam carbon-market publications",
        capabilities=(
            "public_market_data",
            "carbon_allowance",
            "allowance_allocation_volume",
            "trading_calendar",
            "regulatory_reference",
            "source_health",
        ),
        aliases=(
            "hanoi stock exchange",
            "state securities commission",
            "hnx",
            "ssc vietnam",
            "vn2025",
            "vietnam domestic carbon exchange",
            "greenhouse gas emission allowance",
        ),
        docs_url=CARBON_PORTAL_URL,
        runtime_entrypoint=(
            "adapters.venues.hanoi_stock_exchange_state_securities_commission."
            "HanoiStockExchangeStateSecuritiesCommissionAdapter"
        ),
        quote_assets=("VND_PER_TCO2E",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 730.0)))
        urls = {
            "carbon_portal": str(cfg.get("carbon_portal_url") or cfg.get("source_url") or CARBON_PORTAL_URL),
            "trading_notice": str(cfg.get("trading_notice_url") or TRADING_NOTICE_URL),
            "ssc_portal": str(cfg.get("ssc_portal_url") or SSC_PORTAL_URL),
            "ssc_decree": str(cfg.get("ssc_decree_url") or SSC_DECREE_URL),
        }
        results = {
            "carbon_portal": fetch_text(urls["carbon_portal"], timeout),
            "trading_notice": fetch_text(urls["trading_notice"], timeout),
            "ssc_portal": fetch_text(urls["ssc_portal"], timeout),
            "ssc_decree": fetch_bytes(urls["ssc_decree"], timeout),
        }
        parsers = {
            "carbon_portal": lambda result: parse_hnx_carbon_portal(
                str(result.get("text") or ""), source_url=urls["carbon_portal"], received_at=result.get("received_at")
            ),
            "trading_notice": lambda result: parse_hnx_vn2025_trading_notice(
                str(result.get("text") or ""),
                source_url=urls["trading_notice"],
                received_at=result.get("received_at"),
                stale_after_days=stale_after_days,
            ),
            "ssc_portal": lambda result: parse_ssc_portal(
                str(result.get("text") or ""), source_url=urls["ssc_portal"], received_at=result.get("received_at")
            ),
            "ssc_decree": lambda result: parse_ssc_decree_publication(
                bytes(result.get("content") or b""), source_url=urls["ssc_decree"], received_at=result.get("received_at")
            ),
        }
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        usable_sources = 0
        for label, result in results.items():
            if not result.get("ok"):
                observations.append(_failure_observation(result, urls[label], label))
                continue
            try:
                observations.extend(parsers[label](result))
                usable_sources += 1
            except (HanoiSscCarbonParseError, TypeError, ValueError) as exc:
                message = f"{label} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": urls[label], "error": message})
                observations.append(_failure_observation(result, urls[label], label, message))

        if usable_sources == len(urls) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        else:
            statuses = {str(result.get("status") or "unavailable") for result in results.values()}
            source_status = "blocked" if "blocked" in statuses else "unavailable"
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 839,
                "source_status": source_status,
                "source_url": urls["carbon_portal"],
                "source_urls": list(urls.values()),
                "fetch_status": {label: _fetch_evidence(results[label], urls[label]) for label in urls},
                "freshness_state": "fresh" if "fresh" in freshness_states else "stale" if "stale" in freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": sum(1 for row in observations if row.get("quality_status") != "source_health"),
                "capability_gap": "public_entry_quality_vn2025_prices_order_book_and_route",
                "paper_only": True,
            },
        )


# Friendly aliases for callers that refer to the announcement rather than its venue.
parse_hnx_vn2025_allowance_notice = parse_hnx_vn2025_trading_notice
HanoiStockExchangeSscAdapter = HanoiStockExchangeStateSecuritiesCommissionAdapter


register_adapter(HanoiStockExchangeStateSecuritiesCommissionAdapter())
