"""Public California Cap-and-Invest auction and reserve-sale references.

CARB publishes auction notices and completed-result documents, rather than an
anonymous executable allowance market.  This plugin intentionally normalizes
those official records as watch-only research evidence: it never exposes a
broker route, a tradable candidate, or an order instruction.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


AUCTION_INFORMATION_URL = (
    "https://ww2.arb.ca.gov/our-work/programs/cap-and-trade-program/auction-information"
)
DATA_DASHBOARD_FILES_URL = (
    "https://ww2.arb.ca.gov/resources/documents/cap-and-trade-program-data-dashboard-files"
)
RESULTS_SUMMARY_URL = "https://ww2.arb.ca.gov/sites/default/files/2020-08/results_summary.pdf"
MAY_2026_NOTICE_URL = "https://ww2.arb.ca.gov/sites/default/files/2026-03/nc-may_2026_notice.pdf"
MARKET_SURFACE = "california_quebec_cap_and_invest_joint_allowance_auctions"
CARB_TIME = dt.timezone(dt.timedelta(hours=-8), name="America/Los_Angeles_standard")


class CaliforniaAirResourcesBoardParseError(ValueError):
    """Raised when a reachable CARB public document no longer has expected facts."""


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
        raise CaliforniaAirResourcesBoardParseError("official HTML response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain source/parser evidence.
        raise CaliforniaAirResourcesBoardParseError(f"invalid official HTML response: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).replace("\xa0", " ").split())
    if not text:
        raise CaliforniaAirResourcesBoardParseError("official HTML response has no visible text")
    return text


def extract_pdf_text(body: bytes) -> str:
    """Extract text from a bounded public CARB PDF using the existing dependency."""

    if not isinstance(body, bytes) or not body:
        raise CaliforniaAirResourcesBoardParseError("official PDF response is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise CaliforniaAirResourcesBoardParseError("pypdf is required to read CARB public PDFs") from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - document revisions must become source evidence.
        raise CaliforniaAirResourcesBoardParseError(f"official PDF could not be read: {exc}") from exc
    if not text.strip():
        raise CaliforniaAirResourcesBoardParseError("official PDF contains no extractable text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaliforniaAirResourcesBoardParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date(text: str) -> dt.date | None:
    for match in re.finditer(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(20\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        try:
            return dt.datetime.strptime(" ".join(match.groups()), "%B %d %Y").date()
        except ValueError:
            continue
    return None


def _auction_identity(text: str) -> tuple[str, int | None, dt.date | None]:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(20\d{2})\s+(?:California[- ]Québec\s+)?Joint\s+Auction\s*#?\s*(\d+)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise CaliforniaAirResourcesBoardParseError("Joint Auction month/year marker was not found")
    month_name, year_text, number_text = match.groups()
    try:
        month = dt.datetime.strptime(month_name.title(), "%B").month
        year = int(year_text)
    except ValueError as exc:
        raise CaliforniaAirResourcesBoardParseError("Joint Auction month/year is invalid") from exc
    event_date = _date(text[match.end() :]) or dt.date(year, month, 1)
    auction_number = int(number_text) if number_text else None
    label = f"{month_name.title()} {year} Joint Auction" + (f" #{auction_number}" if auction_number else "")
    return label, auction_number, event_date


def _freshness(event_date: dt.date, fetched_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    age = max(0.0, (fetched_at.date() - event_date).days * 86400.0)
    return (
        "fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale",
        round(age, 3),
    )


def _auction_observation(
    *,
    auction_label: str,
    auction_number: int | None,
    event_date: dt.date,
    allowance_category: str,
    market_type: str,
    source_url: str,
    fetched_at: dt.datetime,
    freshness_state: str,
    freshness_age_seconds: float,
    session_status: str,
    quality_status: str,
    price: float | None = None,
    offered: float | None = None,
    sold: float | None = None,
    **details: Any,
) -> dict[str, Any]:
    auction_token = str(auction_number) if auction_number is not None else re.sub(r"[^A-Z0-9]+", "_", auction_label.upper())
    category = allowance_category.upper().replace(" ", "_")
    inst_id = f"CARB:CA_QC_AUCTION:{auction_token}:{category}:{event_date.isoformat()}"
    return {
        "venue": "CARB_CA_QC",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": f"CA_QC_{category}_{auction_token}",
        "name": f"California-Québec {auction_label} {allowance_category.title()} allowance",
        "base": "CA_QC_GHG_ALLOWANCE",
        "quote": "USD_PER_ALLOWANCE",
        "market_type": market_type,
        "market_surface": MARKET_SURFACE,
        "asset_class": "greenhouse_gas_emission_allowance",
        "trade_type": "official_market_reference",
        "direction": "watch_only",
        "last": price if price is not None else 0.0,
        "price_available": price is not None,
        "auction_settlement_price_usd": price,
        "allowances_offered": offered,
        "allowances_sold": sold,
        "allowance_category": allowance_category,
        "auction_number": auction_number,
        "auction_label": auction_label,
        "jurisdictions": ("California", "Québec"),
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": quality_status,
        "freshness_state": freshness_state,
        "freshness_basis": "official_auction_event_date",
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "event_date": event_date.isoformat(),
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "California Air Resources Board public Cap-and-Invest record",
        "source_url": source_url,
        "source_auction_information_url": AUCTION_INFORMATION_URL,
        "candidate_reject_reason": "official_allowance_auction_reference_not_order_routable",
        **details,
    }


def _categories(text: str) -> list[str]:
    categories: list[str] = []
    for label, pattern in (
        ("current", r"\bCurrent\s+Auction\b"),
        ("advance", r"\bAdvance\s+Auction\b"),
        ("reserve", r"\b(?:Reserve\s+Sale|Price\s+Containment\s+Reserve)\b"),
    ):
        if re.search(pattern, text, flags=re.IGNORECASE):
            categories.append(label)
    return categories


def _numeric_after(block: str, label: str) -> float | None:
    match = re.search(label + r"[^0-9$]{0,90}(\$?\s*[0-9][0-9,.]*)", block, flags=re.IGNORECASE)
    return number(match.group(1)) if match else None


def _category_block(text: str, category: str) -> str:
    label = re.escape(category.title())
    match = re.search(rf"\b{label}\s+(?:Auction|Sale)\b", text, flags=re.IGNORECASE)
    if not match:
        return text
    # Result documents repeat e.g. "Current Auction Settlement Price" inside
    # a Current Auction block.  Only a different category starts a new block.
    boundary = len(text)
    for candidate in re.finditer(r"\b(Current|Advance|Reserve)\s+(?:Auction|Sale)\b", text, re.I):
        if candidate.start() > match.start() and candidate.group(1).casefold() != category.casefold():
            boundary = candidate.start()
            break
    return text[match.start() : boundary]


def parse_carb_auction_information(
    document: str,
    *,
    source_url: str = AUCTION_INFORMATION_URL,
    received_at: str | None = None,
    stale_after_days: float = 120.0,
) -> list[dict[str, Any]]:
    """Normalize the current official joint-auction schedule and document state."""

    text = _visible_text(document)
    required = ("Auction Information", "Joint", "Auction")
    if not all(term.casefold() in text.casefold() for term in required):
        raise CaliforniaAirResourcesBoardParseError("Auction Information page markers were not found")
    auction_label, auction_number, event_date = _auction_identity(text)
    categories = _categories(text) or ["current", "advance"]
    fetched_at = _received_time(received_at)
    freshness_state, age = _freshness(event_date, fetched_at, stale_after_days)
    notice_published = bool(re.search(r"(?:Updated\s+)?Auction\s+Notice", text, flags=re.IGNORECASE))
    results_published = bool(re.search(r"Summary\s+Results\s+Report", text, flags=re.IGNORECASE))
    rows = []
    for category in categories:
        rows.append(
            _auction_observation(
                auction_label=auction_label,
                auction_number=auction_number,
                event_date=event_date,
                allowance_category=category,
                market_type="joint_allowance_auction_schedule",
                source_url=source_url,
                fetched_at=fetched_at,
                freshness_state=freshness_state,
                freshness_age_seconds=age,
                session_status="results_published" if results_published else "notice_published" if notice_published else "scheduled",
                quality_status="official_auction_schedule",
                auction_notice_published=notice_published,
                summary_results_report_published=results_published,
                notice_schedule_days_before_auction=60,
            )
        )
    return rows


def parse_carb_auction_results(
    text: str,
    *,
    source_url: str = RESULTS_SUMMARY_URL,
    received_at: str | None = None,
    stale_after_days: float = 180.0,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Normalize settlement price, quantity, and vintage-category result records."""

    if not isinstance(text, str) or not text.strip():
        raise CaliforniaAirResourcesBoardParseError("auction results document is empty")
    if "settlement price" not in text.casefold() or "joint auction" not in text.casefold():
        raise CaliforniaAirResourcesBoardParseError("auction results markers were not found")
    fetched_at = _received_time(received_at)
    starts = list(
        re.finditer(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(20\d{2})\s+Joint\s+Auction\s*#?\s*(\d+)?",
            text,
            flags=re.IGNORECASE,
        )
    )
    if not starts:
        raise CaliforniaAirResourcesBoardParseError("auction results has no Joint Auction headings")
    rows: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        block = text[start.start() : starts[index + 1].start() if index + 1 < len(starts) else None]
        auction_label, auction_number, event_date = _auction_identity(block)
        freshness_state, age = _freshness(event_date, fetched_at, stale_after_days)
        for category in _categories(block):
            category_text = _category_block(block, category)
            price = _numeric_after(category_text, r"Settlement\s+Price")
            offered = _numeric_after(category_text, r"(?:Total\s+)?(?:Auction\s+)?Allowances\s+Offered")
            sold = _numeric_after(category_text, r"(?:Total\s+)?(?:Auction\s+)?Allowances\s+Sold")
            if price is None and offered is None and sold is None:
                continue
            rows.append(
                _auction_observation(
                    auction_label=auction_label,
                    auction_number=auction_number,
                    event_date=event_date,
                    allowance_category=category,
                    market_type="joint_allowance_auction_result",
                    source_url=source_url,
                    fetched_at=fetched_at,
                    freshness_state=freshness_state,
                    freshness_age_seconds=age,
                    session_status="closed",
                    quality_status="official_auction_result",
                    price=price,
                    offered=offered,
                    sold=sold,
                    result_document_type="summary_settlement_prices_and_results",
                )
            )
    if not rows:
        raise CaliforniaAirResourcesBoardParseError("auction results has no usable category price or quantity rows")
    rows.sort(key=lambda row: (str(row["event_date"]), str(row["allowance_category"])), reverse=True)
    return rows[: max(1, int(limit))]


def parse_carb_auction_notice(
    text: str,
    *,
    source_url: str = MAY_2026_NOTICE_URL,
    received_at: str | None = None,
    stale_after_days: float = 90.0,
) -> list[dict[str, Any]]:
    """Normalize current/advance/reserve notice evidence and disclosed vintages."""

    if not isinstance(text, str) or not text.strip():
        raise CaliforniaAirResourcesBoardParseError("auction notice document is empty")
    if "auction" not in text.casefold() or "california" not in text.casefold():
        raise CaliforniaAirResourcesBoardParseError("auction notice markers were not found")
    auction_label, auction_number, event_date = _auction_identity(text)
    categories = _categories(text)
    if not categories:
        raise CaliforniaAirResourcesBoardParseError("auction notice has no current, advance, or reserve category")
    fetched_at = _received_time(received_at)
    freshness_state, age = _freshness(event_date, fetched_at, stale_after_days)
    rows = []
    for category in categories:
        block = _category_block(text, category)
        vintages = sorted(set(re.findall(r"\b(?:vintage\s+)?(20\d{2})\b", block)))
        rows.append(
            _auction_observation(
                auction_label=auction_label,
                auction_number=auction_number,
                event_date=event_date,
                allowance_category=category,
                market_type="joint_allowance_auction_notice",
                source_url=source_url,
                fetched_at=fetched_at,
                freshness_state=freshness_state,
                freshness_age_seconds=age,
                session_status="notice_published",
                quality_status="official_auction_notice",
                vintage_years=tuple(int(value) for value in vintages),
                vintage_disclosure_available=bool(vintages),
                reserve_sale=(category == "reserve"),
                notice_schedule_days_before_auction=60,
            )
        )
    return rows


def parse_carb_data_dashboard_files(
    document: str,
    *,
    source_url: str = DATA_DASHBOARD_FILES_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Record the official Cap-and-Trade data-dashboard publication surface."""

    text = _visible_text(document)
    if "cap-and-trade" not in text.casefold() or "data dashboard" not in text.casefold():
        raise CaliforniaAirResourcesBoardParseError("Cap-and-Trade data dashboard markers were not found")
    fetched_at = _received_time(received_at)
    return [
        {
            "venue": "CARB_CA_QC",
            "inst_id": "CARB:CAP_AND_INVEST:DATA_DASHBOARD",
            "instrument_id": "CARB:CAP_AND_INVEST:DATA_DASHBOARD",
            "symbol": "CARB_DATA_DASHBOARD",
            "name": "California Cap-and-Invest Program data dashboard files",
            "base": "CA_QC_GHG_ALLOWANCE",
            "quote": "USD",
            "market_type": "official_data_publication_catalog",
            "market_surface": MARKET_SURFACE,
            "asset_class": "greenhouse_gas_emission_allowance",
            "trade_type": "official_market_reference",
            "direction": "watch_only",
            "last": 0.0,
            "price_available": False,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_data_dashboard_catalog",
            "freshness_state": "fresh",
            "freshness_basis": "official_dashboard_page_fetch",
            "freshness_age_seconds": 0.0,
            "session_status": "reference_only",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "California Air Resources Board public data dashboard",
            "source_url": source_url,
            "candidate_reject_reason": "public_data_catalog_not_executable_quote",
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
    source_key: str, source_url: str, result: dict[str, Any], parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("CARB_CA_QC", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"CARB:CA_QC:{source_key.upper()}:HEALTH",
            "instrument_id": f"CARB:CA_QC:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "CA_QC_GHG_ALLOWANCE",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_carb_parser_failure" if parser_error else "public_carb_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class CaliforniaAirResourcesBoardAdapter:
    info = AdapterInfo(
        adapter_id="california_air_resources_board_cap_and_invest",
        venue="CARB_CA_QC",
        market_type="greenhouse_gas_allowance_auction",
        source="California Air Resources Board public California-Québec Cap-and-Invest auction records",
        capabilities=(
            "public_market_data",
            "auction_schedule",
            "auction_notice",
            "auction_results",
            "auction_settlement_price",
            "event_price_reference",
            "allowance_volume",
            "allowance_vintage",
            "reserve_sale",
            "proceeds_publication_reference",
            "source_health",
        ),
        aliases=(
            "california air resources board",
            "carb",
            "california cap and invest",
            "california cap and trade",
            "california quebec joint auction",
            "ghg allowance auction",
            "price containment reserve sale",
        ),
        docs_url=AUCTION_INFORMATION_URL,
        runtime_entrypoint=(
            "adapters.venues.california_air_resources_board.CaliforniaAirResourcesBoardAdapter"
        ),
        quote_assets=("USD_PER_ALLOWANCE",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 180.0)))
        result_limit = max(1, min(int(cfg.get("max_result_rows", 24)), 100))
        urls = {
            "auction_information": str(cfg.get("auction_information_url") or AUCTION_INFORMATION_URL),
            "dashboard_files": str(cfg.get("dashboard_files_url") or DATA_DASHBOARD_FILES_URL),
            "results_summary": str(cfg.get("results_summary_url") or RESULTS_SUMMARY_URL),
            "auction_notice": str(cfg.get("auction_notice_url") or MAY_2026_NOTICE_URL),
        }
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0
        text_sources = (
            ("auction_information", parse_carb_auction_information),
            ("dashboard_files", parse_carb_data_dashboard_files),
        )
        for source_key, parser in text_sources:
            source_url = urls[source_key]
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                kwargs: dict[str, Any] = {"source_url": source_url, "received_at": result.get("received_at")}
                if source_key == "auction_information":
                    kwargs["stale_after_days"] = stale_after_days
                observations.extend(parser(str(result.get("text") or ""), **kwargs))
                usable_sources += 1
            except (CaliforniaAirResourcesBoardParseError, TypeError, ValueError) as exc:
                message = f"CARB {source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        pdf_sources = (("results_summary", parse_carb_auction_results), ("auction_notice", parse_carb_auction_notice))
        for source_key, parser in pdf_sources:
            source_url = urls[source_key]
            result = fetch_bytes(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                # Supplying text directly is useful for deterministic tests and cached extractors.
                text = str(result["text"]) if "text" in result else extract_pdf_text(result.get("content") or b"")
                kwargs = {
                    "source_url": source_url,
                    "received_at": result.get("received_at"),
                    "stale_after_days": stale_after_days,
                }
                if source_key == "results_summary":
                    kwargs["limit"] = result_limit
                observations.extend(parser(text, **kwargs))
                usable_sources += 1
            except (CaliforniaAirResourcesBoardParseError, TypeError, ValueError) as exc:
                message = f"CARB {source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(fetch_status) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1079,
                "source_status": source_status,
                "source_url": urls["auction_information"],
                "source_urls": list(urls.values()),
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_entry_quality_allowance_quotes_and_order_book_not_available",
                "paper_only": True,
            },
        )


register_adapter(CaliforniaAirResourcesBoardAdapter())
