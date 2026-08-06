"""Public ICDX GOFX and CPOTR surface adapter.

ICDX publishes public no-key venue descriptions for GOFX and CPOTR, plus a
homepage price card for the current CPOTR contract month. These pages do not
expose a full executable quote or order-entry feed, so normalized observations
remain paper-only and route-needed.
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


HOME_URL = "https://www.icdx.co.id/"
EXCHANGE_URL = "https://www.icdx.co.id/our-market/exchange"
CPO_URL = "https://www.icdx.co.id/our-market/cpo-physical-market"
ABOUT_URL = "https://www.icdx.co.id/about-us"
VENUE = "ICDX"
MARKET_SURFACE = "icdx_gofx_cpotr_public_reference"


class IcdxParseError(ValueError):
    """Raised when a reachable ICDX page loses required venue markers."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _visible_text(payload: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise IcdxParseError("ICDX response must be non-empty HTML text")
    parser = _VisibleTextParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - preserve source-health evidence.
        raise IcdxParseError(f"invalid ICDX HTML: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).split())
    if not text:
        raise IcdxParseError("ICDX response has no visible text")
    return text


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IcdxParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _number(value: str) -> float | None:
    text = re.sub(r"[^0-9,.\-]", "", str(value or ""))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif text.count(".") > 1 or re.search(r"\.\d{3}(?:$|\.)", text):
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _reference_row(
    *,
    inst_id: str,
    symbol: str,
    name: str,
    base: str,
    quote: str,
    market_type: str,
    market_surface: str,
    asset_class: str,
    trade_type: str,
    source_url: str,
    fetched_at: dt.datetime,
    notes: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": base,
        "quote": quote,
        "market_type": market_type,
        "market_surface": market_surface,
        "asset_class": asset_class,
        "trade_type": trade_type,
        "direction": "watch_only",
        "last": 0.0,
        "data_access_type": "public_no_key",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_market_reference",
        "freshness_state": "fresh",
        "freshness_basis": "official_page_fetch_time",
        "freshness_age_seconds": 0.0,
        "session_status": "reference_only",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Indonesia Commodity & Derivatives Exchange (ICDX) public market pages",
        "source_url": source_url,
        "notes": notes,
        "paper_route_status": "synthetic_research_only",
        "execution_route_status": "route_needed",
        "candidate_reject_reason": "public_reference_not_entry_quality_quote",
    }
    if extra:
        row.update(extra)
    return row


def parse_icdx_exchange_surface(
    payload: str,
    *,
    source_url: str = EXCHANGE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    text = _visible_text(payload)
    normalized = " ".join(text.split())
    required_markers = (
        "GOFX is a suite of futures products encompassing Gold, Oil, and Forex",
        "Commodity Futures Trading Regulatory Agency (CoFTRA)",
        "MetaTrader 5",
        "Spot Gold contracts in three sizes: 1 gram, 1 ounce, and 10 ounces",
        "Crude Oil Futures",
        "spot forex contracts",
    )
    missing = [marker for marker in required_markers if marker not in normalized]
    if missing:
        raise IcdxParseError("ICDX exchange page is missing marker(s): " + "; ".join(missing))
    fetched_at = _received_at(received_at)
    common = {
        "product_group": "GOFX",
        "regulator": "CoFTRA",
        "platform": "MetaTrader 5",
        "venue_model": "multilateral",
    }
    return [
        _reference_row(
            inst_id=f"{VENUE}:GOFX:GOLD",
            symbol="GOFX_GOLD",
            name="ICDX GOFX Gold rolling spot surface",
            base="GOLD",
            quote="USD_IDR_REFERENCE",
            market_type="gold_reference_surface",
            market_surface="icdx_gofx_gold",
            asset_class="gold_rolling_spot",
            trade_type="official_market_surface_reference",
            source_url=source_url,
            fetched_at=fetched_at,
            notes=["ICDX states GOFX gold offers spot gold contracts in 1 gram, 1 ounce, and 10 ounces."],
            extra={**common, "contract_sizes": ["1_GRAM", "1_OUNCE", "10_OUNCES"]},
        ),
        _reference_row(
            inst_id=f"{VENUE}:GOFX:CRUDE_OIL",
            symbol="GOFX_CRUDE_OIL",
            name="ICDX GOFX crude oil futures surface",
            base="CRUDE_OIL",
            quote="USD_PER_BBL_REFERENCE",
            market_type="commodity_futures_reference_surface",
            market_surface="icdx_gofx_crude_oil",
            asset_class="crude_oil_futures",
            trade_type="official_market_surface_reference",
            source_url=source_url,
            fetched_at=fetched_at,
            notes=["ICDX states GOFX includes crude oil futures in its multilateral venue."],
            extra=common,
        ),
        _reference_row(
            inst_id=f"{VENUE}:GOFX:FOREX",
            symbol="GOFX_FOREX",
            name="ICDX GOFX forex surface",
            base="FOREX",
            quote="MULTI_CCY_REFERENCE",
            market_type="forex_reference_surface",
            market_surface="icdx_gofx_forex",
            asset_class="forex_contracts",
            trade_type="official_market_surface_reference",
            source_url=source_url,
            fetched_at=fetched_at,
            notes=["ICDX states GOFX includes exchange-traded spot forex contracts on the multilateral venue."],
            extra=common,
        ),
    ]


def parse_icdx_cpotr_reference(
    payload: str,
    *,
    source_url: str = CPO_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    text = _visible_text(payload)
    required_markers = (
        "launched the CPOTR futures contract in 2010",
        "reference for Indonesian CPO prices",
        "physical CPO trading through an exchange auction mechanism",
    )
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise IcdxParseError("ICDX CPO page is missing marker(s): " + "; ".join(missing))
    fetched_at = _received_at(received_at)
    return [
        _reference_row(
            inst_id=f"{VENUE}:CPOTR:REFERENCE",
            symbol="CPOTR",
            name="ICDX CPOTR crude palm oil futures reference surface",
            base="CRUDE_PALM_OIL",
            quote="IDR_PER_TONNE",
            market_type="commodity_futures_reference_surface",
            market_surface="icdx_cpotr",
            asset_class="crude_palm_oil_futures",
            trade_type="official_market_surface_reference",
            source_url=source_url,
            fetched_at=fetched_at,
            notes=["ICDX states CPOTR launched in 2010 and serves as a reference for Indonesian CPO prices."],
            extra={
                "benchmark_since_year": 2010,
                "price_reference_role": "indonesian_cpo_reference",
                "hedging_use_case": "producer_exporter_buyer_price_risk_management",
            },
        )
    ]


def parse_icdx_about_milestones(
    payload: str,
    *,
    source_url: str = ABOUT_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    text = _visible_text(payload)
    required_markers = (
        "2009 Indonesia Commodity & Derivatives Exchange (ICDX) was established.",
        "2010 CPOTR futures contract launch as a benchmark price for CPO exporter in Indonesia.",
        "2018 The launch of GOFX as the first regulated exchange-traded rolling spot and futures platform in ASEAN.",
        "2019 The launch of GOFX Micro.",
        "2020 The launch of COFR, COFU10, and COFU100 contracts.",
    )
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise IcdxParseError("ICDX about-us page is missing marker(s): " + "; ".join(missing))
    fetched_at = _received_at(received_at)
    return [
        _reference_row(
            inst_id=f"{VENUE}:MARKET_MILESTONES",
            symbol="ICDX_MILESTONES",
            name="ICDX GOFX and CPOTR milestone timeline",
            base="ICDX",
            quote="N/A",
            market_type="exchange_milestone_reference",
            market_surface="icdx_exchange_milestones",
            asset_class="exchange_development",
            trade_type="official_market_milestone_reference",
            source_url=source_url,
            fetched_at=fetched_at,
            notes=[
                "ICDX milestone page dates CPOTR launch to 2010, GOFX launch to 2018, GOFX Micro to 2019, and COFR/COFU10/COFU100 launches to 2020."
            ],
            extra={
                "exchange_established_year": 2009,
                "cpotr_launch_year": 2010,
                "gofx_launch_year": 2018,
                "gofx_micro_launch_year": 2019,
                "crude_oil_contract_launch_year": 2020,
                "crude_oil_contract_codes": ["COFR", "COFU10", "COFU100"],
            },
        )
    ]


def parse_icdx_homepage_price_cards(
    payload: str,
    *,
    source_url: str = HOME_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    text = _visible_text(payload)
    match = re.search(
        r"SOBO\s+CPOTR\s+([A-Z]{3}\d{2})\s+\(Suggested Opening\)\s+([0-9][0-9.,]*)\s+"
        r"YDSP\s+CPOTR(?:\s+([A-Z]{3}\d{2}))?\s+\(Previous Settlement\)\s+([0-9][0-9.,]*)",
        text,
    )
    if not match:
        raise IcdxParseError("ICDX homepage CPOTR price card markers were not found")
    contract_month = match.group(1)
    settlement_month = match.group(3) or contract_month
    suggested_opening = _number(match.group(2))
    previous_settlement = _number(match.group(4))
    if suggested_opening is None or suggested_opening <= 0:
        raise IcdxParseError("ICDX suggested opening value is missing or invalid")
    if previous_settlement is None or previous_settlement <= 0:
        raise IcdxParseError("ICDX previous settlement value is missing or invalid")
    fetched_at = _received_at(received_at)
    common = {
        "venue": VENUE,
        "symbol": "CPOTR",
        "base": "CRUDE_PALM_OIL",
        "quote": "IDR_PER_TONNE",
        "market_type": "commodity_futures_reference_price",
        "market_surface": "icdx_cpotr",
        "asset_class": "crude_palm_oil_futures",
        "direction": "watch_only",
        "data_access_type": "public_no_key",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_price_card",
        "freshness_state": "fresh",
        "freshness_basis": "official_homepage_fetch_time",
        "freshness_age_seconds": 0.0,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "ICDX homepage official price card",
        "source_url": source_url,
        "source_record_type": "homepage_price_card",
        "price_reference_role": "official_price_card",
        "paper_route_status": "synthetic_research_only",
        "execution_route_status": "route_needed",
        "candidate_reject_reason": "public_price_card_not_execution_route",
    }
    return [
        {
            **common,
            "inst_id": f"{VENUE}:CPOTR:{contract_month}:SOBO",
            "instrument_id": f"{VENUE}:CPOTR:{contract_month}:SOBO",
            "name": f"ICDX CPOTR {contract_month} suggested opening",
            "trade_type": "official_price_card_reference",
            "last": suggested_opening,
            "contract_month": contract_month,
            "price_type": "suggested_opening",
            "price_basis": "suggested_opening_idr_per_tonne",
            "session_status": "pre_open_indicative",
        },
        {
            **common,
            "inst_id": f"{VENUE}:CPOTR:{settlement_month}:YDSP",
            "instrument_id": f"{VENUE}:CPOTR:{settlement_month}:YDSP",
            "name": f"ICDX CPOTR {settlement_month} previous settlement",
            "trade_type": "official_price_card_reference",
            "last": previous_settlement,
            "contract_month": settlement_month,
            "price_type": "previous_settlement",
            "price_basis": "previous_settlement_idr_per_tonne",
            "session_status": "previous_settlement_reference",
        },
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
    source_key: str,
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "ICDX_PUBLIC_MARKET_REFERENCE",
            "quote": "N/A",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": "public_icdx_parser_failure" if parser_error else "public_icdx_source_unavailable",
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class IndonesiaCommodityDerivativesExchangeIcdxAdapter:
    info = AdapterInfo(
        adapter_id="indonesia_commodity_derivatives_exchange_icdx",
        venue=VENUE,
        market_type="commodity_and_fx_reference_surface",
        source="ICDX public GOFX, CPOTR, and venue milestone pages",
        capabilities=(
            "public_market_data",
            "commodity_derivatives",
            "gold_reference_surface",
            "crude_oil_reference_surface",
            "forex_reference_surface",
            "crude_palm_oil_reference_surface",
            "official_price_card",
            "source_health",
        ),
        aliases=(
            "indonesia commodity and derivatives exchange",
            "icdx",
            "gofx",
            "cpotr",
            "indonesia commodity derivatives exchange",
        ),
        docs_url=EXCHANGE_URL,
        runtime_entrypoint=(
            "adapters.venues.indonesia_commodity_derivatives_exchange_icdx."
            "IndonesiaCommodityDerivativesExchangeIcdxAdapter"
        ),
        quote_assets=("IDR_PER_TONNE", "USD_PER_BBL_REFERENCE", "USD_IDR_REFERENCE", "MULTI_CCY_REFERENCE"),
        default_cache_minutes=120,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        home_url = str(cfg.get("home_url") or HOME_URL)
        exchange_url = str(cfg.get("exchange_url") or EXCHANGE_URL)
        cpo_url = str(cfg.get("cpo_url") or CPO_URL)
        about_url = str(cfg.get("about_url") or ABOUT_URL)

        results = {
            "homepage": (home_url, fetch_text(home_url, timeout)),
            "exchange": (exchange_url, fetch_text(exchange_url, timeout)),
            "cpo_physical_market": (cpo_url, fetch_text(cpo_url, timeout)),
            "about_us": (about_url, fetch_text(about_url, timeout)),
        }
        fetch_status = {key: _fetch_evidence(result, url) for key, (url, result) in results.items()}
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        parsers = {
            "homepage": parse_icdx_homepage_price_cards,
            "exchange": parse_icdx_exchange_surface,
            "cpo_physical_market": parse_icdx_cpotr_reference,
            "about_us": parse_icdx_about_milestones,
        }
        for source_key, parser in parsers.items():
            source_url, result = results[source_key]
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                observations.extend(
                    parser(
                        str(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                    )
                )
            except (IcdxParseError, TypeError, ValueError) as exc:
                message = f"ICDX {source_key.replace('_', ' ')} parser failed: {exc}"[:300]
                parser_failures.append({"source": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        statuses = [evidence["fetch_status"] for evidence in fetch_status.values()]
        if real_rows and all(status == "reachable" for status in statuses) and not parser_failures:
            source_status = "reachable"
        elif real_rows or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        sessions = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 456,
                "source_status": source_status,
                "source_url": exchange_url,
                "source_urls": [exchange_url, cpo_url, about_url, home_url],
                "fetch_status": fetch_status,
                "freshness_state": freshness[0] if len(freshness) == 1 else ("mixed" if freshness else "unknown"),
                "session_state": sessions[0] if len(sessions) == 1 else ("mixed" if sessions else "unknown"),
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_entry_quality_quotes_for_full_gofx_suite_and_order_routing",
                "paper_only": True,
            },
        )


register_adapter(IndonesiaCommodityDerivativesExchangeIcdxAdapter())
