"""Public California--Québec joint cap-and-invest auction references.

California Air Resources Board (CARB) publishes the auction notices and the
subsequent settlement summaries without authentication.  Those publications
are useful supply and price references, but are not an executable market data
feed.  This adapter therefore deliberately emits watch-only observations.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, slug, utc_now
from scan_batch import ScanBatch


NOTICE_REPORTS_URL = (
    "https://ww2.arb.ca.gov/our-work/programs/cap-and-trade-program/"
    "auction-information/auction-notices-and-reports"
)
SUMMARY_RESULTS_URL = "https://ww2.arb.ca.gov/resources/documents/summary-auction-settlement-prices-and-results"
PRINTABLE_RESULT_URL = (
    "https://ww2.arb.ca.gov/news/california-and-quebec-release-summary-results-"
    "47th-joint-cap-and-invest-allowance-auction/printable/print"
)
# Short aliases make configuration and external callers independent of the
# particular CARB page used as the default result document.
SOURCE_URL = PRINTABLE_RESULT_URL
MARKET_SURFACE = "california_quebec_joint_cap_and_invest_allowance_auctions"


class CaliforniaQuebecAuctionParseError(ValueError):
    """Raised when a reachable official auction page has no usable auction data."""


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
        raise CaliforniaQuebecAuctionParseError("auction document is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain a source-health observation.
        raise CaliforniaQuebecAuctionParseError(f"invalid auction HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_time(value: str | None) -> dt.datetime:
    raw = value or utc_now()
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaliforniaQuebecAuctionParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _date_from_text(text: str) -> dt.datetime | None:
    patterns = (
        r"(?:Auction\s+Date|Auction\s+(?:was|will be)\s+held(?:\s+on)?|held\s+on)\s*[:,-]?\s*"
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+(?:Joint\s+)?(?:Cap-and-(?:Trade|Invest)|Allowance)\s+Auction",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return dt.datetime.strptime(match.group(1), "%B %d, %Y").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
    return None


def _metric(section: str, labels: tuple[str, ...], *, money: bool = False) -> float | None:
    value = r"\$?\s*([0-9][0-9,]*(?:\.\d+)?)" if money else r"([0-9][0-9,]*(?:\.\d+)?)"
    for label in labels:
        match = re.search(rf"{label}\s*[:=-]?\s*{value}", section, re.IGNORECASE)
        if match:
            return _number(match.group(1))
    return None


def _auction_section(text: str, vintage: str) -> str:
    """Return the part of a notice/result devoted to one auction vintage."""

    marker = rf"\b{vintage}\s+Auction\b"
    match = re.search(marker, text, re.IGNORECASE)
    if not match:
        return ""
    remainder = text[match.start() :]
    other = "Advance" if vintage.lower() == "current" else "Current"
    boundary = re.search(rf"\b{other}\s+Auction\b", remainder[1:], re.IGNORECASE)
    return remainder[: boundary.start() + 1] if boundary else remainder[:1800]


def _vintage_year(section: str) -> int | None:
    match = re.search(r"\b(?:vintage\s+)?(20\d{2})\s+(?:vintage\s+)?allowances?\b", section, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _auction_number(text: str) -> int | None:
    match = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:joint\s+)?(?:cap-and-invest\s+)?allowance\s+auction", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _freshness(event_time: dt.datetime, received_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    age = max(0.0, (received_at - event_time).total_seconds())
    return ("fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale", round(age, 3))


def parse_california_quebec_joint_auction(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_days: float = 120.0,
) -> list[dict[str, Any]]:
    """Normalize current- and advance-vintage CARB/Québec auction facts.

    The official pages use both prose and table labels, so this intentionally
    accepts the documented variants for settlement price, reserve price, and
    allowances offered/available.  At least one price or supply datum is
    required for each emitted vintage.
    """

    text = _visible_text(document)
    normalized = re.sub(r"[\u2010-\u2015]", "-", text)
    if not re.search(r"(?:California\s+and\s+Qu(?:e|\u00e9)bec|Qu(?:e|\u00e9)bec.*?California)", normalized, re.IGNORECASE):
        raise CaliforniaQuebecAuctionParseError("California-Québec joint-auction marker was not found")
    if not re.search(r"(?:cap-and-(?:trade|invest)|allowance)\s+auction", normalized, re.IGNORECASE):
        raise CaliforniaQuebecAuctionParseError("allowance-auction marker was not found")

    fetched_at = _received_time(received_at)
    event_time = _date_from_text(normalized) or fetched_at
    auction_number = _auction_number(normalized)
    rows: list[dict[str, Any]] = []
    for vintage in ("Current", "Advance"):
        section = _auction_section(normalized, vintage)
        if not section:
            continue
        settlement_price = _metric(
            section,
            (r"(?:current|advance)\s+auction\s+(?:settlement|clearing)\s+price", r"(?:settlement|clearing)\s+price"),
            money=True,
        )
        reserve_price = _metric(
            section,
            (r"(?:current|advance)\s+auction\s+reserve\s+price", r"reserve\s+price"),
            money=True,
        )
        allowances = _metric(
            section,
            (
                r"(?:current|advance)\s+auction\s+(?:allowances?\s+)?(?:offered|available(?:\s+for\s+sale)?|supply|volume)",
                r"(?:allowances?\s+)?(?:offered|available(?:\s+for\s+sale)?|sold)",
            ),
        )
        if settlement_price is None and reserve_price is None and allowances is None:
            continue
        vintage_year = _vintage_year(section)
        price = settlement_price if settlement_price is not None else reserve_price or 0.0
        freshness_state, freshness_age = _freshness(event_time, fetched_at, stale_after_days)
        result_published = settlement_price is not None
        vintage_token = vintage.upper()
        auction_token = str(auction_number) if auction_number is not None else event_time.date().isoformat()
        row = {
            "venue": "CARB_QUEBEC",
            "inst_id": f"CARB_QUEBEC:JOINT_AUCTION:{auction_token}:{vintage_token}",
            "instrument_id": f"CARB_QUEBEC:JOINT_AUCTION:{auction_token}:{vintage_token}",
            "symbol": f"CCA_{vintage_token}",
            "name": f"California-Québec joint cap-and-invest {vintage.lower()} auction",
            "base": "CALIFORNIA_QUEBEC_ALLOWANCE",
            "quote": "USD_PER_ALLOWANCE",
            "market_type": "allowance_auction_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "emission_allowance",
            "trade_type": "official_market_reference",
            "direction": "watch_only",
            "last": price,
            "auction_settlement_price_usd": settlement_price,
            "auction_reserve_price_usd": reserve_price,
            "allowances_offered": allowances,
            "auction_vintage": vintage.lower(),
            "vintage_year": vintage_year,
            "auction_number": auction_number,
            "auction_date": event_time.date().isoformat(),
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_joint_auction_result" if result_published else "official_joint_auction_notice",
            "freshness_state": freshness_state,
            "freshness_basis": "official_auction_date",
            "freshness_age_seconds": freshness_age,
            "session_status": "closed" if result_published else "scheduled",
            "observed_at": event_time.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "California Air Resources Board / Québec joint allowance auction publication",
            "source_url": source_url,
            "source_notice_url": NOTICE_REPORTS_URL,
            "source_summary_url": SUMMARY_RESULTS_URL,
            "candidate_reject_reason": "official_allowance_auction_reference_not_executable_quote",
        }
        rows.append(row)
    if not rows:
        raise CaliforniaQuebecAuctionParseError("no usable current or advance auction price/supply data was found")
    return rows


# Compatibility names for callers that use the regulator or report terminology.
parse_carb_quebec_joint_auction = parse_california_quebec_joint_auction
parse_carb_quebec_auction = parse_california_quebec_joint_auction


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(result: dict[str, Any], source_url: str, parser_error: str | None = None) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("CARB_QUEBEC", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"CARB_QUEBEC:ADAPTER_HEALTH:{slug('joint_auction')}",
            "instrument_id": f"CARB_QUEBEC:ADAPTER_HEALTH:{slug('joint_auction')}",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_notice_url": NOTICE_REPORTS_URL,
            "source_summary_url": SUMMARY_RESULTS_URL,
            "candidate_reject_reason": "public_allowance_auction_parser_failure" if parser_error else "public_allowance_auction_source_unavailable",
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class CaliforniaAirResourcesBoardQuebecMinistryAdapter:
    info = AdapterInfo(
        adapter_id="california_air_resources_board_qu_bec_ministry",
        venue="CARB_QUEBEC",
        market_type="emission_allowance_auction",
        source="California Air Resources Board and Québec public joint allowance auction publications",
        capabilities=(
            "public_market_data",
            "allowance_auction",
            "current_vintage",
            "advance_vintage",
            "settlement_price",
            "reserve_price",
            "supply_volume",
            "source_health",
        ),
        aliases=(
            "california air resources board",
            "carb",
            "quebec ministry",
            "california quebec joint auction",
            "cap and invest allowance auction",
            "cap and trade allowance auction",
        ),
        docs_url=NOTICE_REPORTS_URL,
        runtime_entrypoint=(
            "adapters.venues.california_air_resources_board_qu_bec_ministry."
            "CaliforniaAirResourcesBoardQuebecMinistryAdapter"
        ),
        quote_assets=("USD_PER_ALLOWANCE",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 120.0)))
        source_url = str(cfg.get("source_url") or cfg.get("result_url") or SOURCE_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_california_quebec_joint_auction(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                source_status = "reachable"
                states = {str(row["freshness_state"]) for row in observations}
                freshness_state = "fresh" if "fresh" in states else "stale"
                sessions = {str(row["session_status"]) for row in observations}
                session_state = sessions.pop() if len(sessions) == 1 else "mixed"
            except (CaliforniaQuebecAuctionParseError, TypeError, ValueError) as exc:
                message = f"CARB/Qu\u00e9bec joint-auction parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1006,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url, NOTICE_REPORTS_URL, SUMMARY_RESULTS_URL],
                "fetch_status": {"joint_auction_result": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "current_vintage_observation_count": sum(1 for row in real_observations if row.get("auction_vintage") == "current"),
                "advance_vintage_observation_count": sum(1 for row in real_observations if row.get("auction_vintage") == "advance"),
                "capability_gap": "public_entry_quality_secondary_market_quotes_and_order_book",
                "paper_only": True,
            },
        )


register_adapter(CaliforniaAirResourcesBoardQuebecMinistryAdapter())
