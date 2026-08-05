"""Hacienda La Esmeralda's public Esmeralda Auction reference adapter.

The auction's public FAQ announces the timed green-tip Geisha microlot sale
and its rules.  The linked catalogue is a public, client-rendered page, but
placing a bid requires approved-auction-bidder access.  These facts are useful
for paper research, not an executable price feed, so every row remains
watch-only and no order path is exposed.
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


FAQ_URL = "https://auction.haciendaesmeralda.com/faqs"
AUCTION_URL = "https://app.haciendaesmeralda.com/auction/90-years-of-price-peterson"
MARKET_SURFACE = "hacienda_la_esmeralda_esmeralda_auction"
VENUE = "HACIENDA_LA_ESMERALDA"
# The public 2026 event is in August, when New York is UTC-04:00.  A fixed
# offset avoids making plugin discovery depend on an optional system tzdata
# package, which is absent from some minimal Python deployments.
NEW_YORK = dt.timezone(dt.timedelta(hours=-4), name="America/New_York")


class HaciendaLaEsmeraldaParseError(ValueError):
    """Raised when a reachable Esmeralda FAQ no longer carries auction facts."""


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


def _visible_text(payload: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise HaciendaLaEsmeraldaParseError("FAQ response must be non-empty HTML text")
    parser = _VisibleTextParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - caller must retain parser-health evidence.
        raise HaciendaLaEsmeraldaParseError(f"invalid FAQ HTML: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).split())
    if not text:
        raise HaciendaLaEsmeraldaParseError("FAQ response has no visible text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HaciendaLaEsmeraldaParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _auction_time(text: str) -> dt.datetime:
    match = re.search(
        r"upcoming\s+esmeralda\s+auction\s+will\s+be\s+held\s+on\s+"
        r"([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise HaciendaLaEsmeraldaParseError("upcoming auction date was not found")
    try:
        auction_date = dt.datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError as exc:
        raise HaciendaLaEsmeraldaParseError("upcoming auction date is invalid") from exc
    ny_time = re.search(r"\b(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*New\s+York\b", text, re.IGNORECASE)
    time_value = dt.time(5, 0)
    if ny_time:
        try:
            time_value = dt.datetime.strptime(ny_time.group(1).upper().replace(" ", ""), "%I:%M%p").time()
        except ValueError as exc:
            raise HaciendaLaEsmeraldaParseError("New York auction time is invalid") from exc
    return dt.datetime.combine(auction_date, time_value, tzinfo=NEW_YORK)


def _minimum_bid(text: str) -> float:
    match = re.search(r"minimum\s+bid\s+is\s+\$\s*([0-9]+(?:\.\d+)?)\s+per\s+kg", text, re.IGNORECASE)
    if not match:
        raise HaciendaLaEsmeraldaParseError("minimum bid in USD per kg was not found")
    return float(match.group(1))


def _session_status(auction_at: dt.datetime, fetched_at: dt.datetime) -> str:
    if fetched_at.astimezone(dt.timezone.utc) < auction_at.astimezone(dt.timezone.utc):
        return "scheduled"
    return "closed"


def parse_hacienda_la_esmeralda_esmeralda_auction_faq(
    payload: str,
    *,
    source_url: str = FAQ_URL,
    catalogue_url: str = AUCTION_URL,
    received_at: str | None = None,
    stale_after_days: float = 30.0,
) -> list[dict[str, Any]]:
    """Normalize public 2026 Esmeralda Auction facts from the official FAQ."""

    text = _visible_text(payload)
    lowered = text.casefold()
    required_markers = ("esmeralda auction", "green coffee auction", "green-tip geisha")
    if not all(marker in lowered for marker in required_markers):
        raise HaciendaLaEsmeraldaParseError("Esmeralda green-tip Geisha auction markers were not found")
    auction_at = _auction_time(text)
    minimum_bid = _minimum_bid(text)
    fetched_at = _received_time(received_at)
    age = max(0.0, (fetched_at - auction_at.astimezone(dt.timezone.utc)).total_seconds())
    freshness_state = "fresh" if age <= max(0.0, stale_after_days) * 86400 else "stale"
    harvest_months = {
        "Enero": "January",
        "Carnaval": "February",
        "San José": "March",
        "Pascua": "April",
    }
    named_harvest_months = tuple(name for name in harvest_months if name.casefold() in lowered)
    farms = tuple(farm for farm in ("Jaramillo", "El Velo", "Cañas Verdes") if farm.casefold() in lowered)
    if len(named_harvest_months) != len(harvest_months) or len(farms) != 3:
        raise HaciendaLaEsmeraldaParseError("harvest-month or Geisha farm markers were not found")
    session_status = _session_status(auction_at, fetched_at)
    auction_date = auction_at.date().isoformat()
    return [
        {
            "venue": VENUE,
            "inst_id": f"HACIENDA_LA_ESMERALDA:ESMERALDA_AUCTION:{auction_date}",
            "instrument_id": f"HACIENDA_LA_ESMERALDA:ESMERALDA_AUCTION:{auction_date}",
            "symbol": f"ESMERALDA_GEISHA_{auction_at.year}",
            "name": f"Hacienda La Esmeralda Esmeralda Auction {auction_at.year}",
            "base": "PANAMA_GREEN_TIP_GEISHA_MICROLOTS",
            "quote": "USD_PER_KG",
            "market_type": "green_coffee_microlot_auction_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "specialty_green_coffee_microlot",
            "trade_type": "public_auction_reference",
            "direction": "watch_only",
            "last": minimum_bid,
            "price_available": False,
            "minimum_bid_usd_per_kg": minimum_bid,
            "price_basis": "published_minimum_bid_not_market_clearing_price",
            "data_access_type": "public_no_key",
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_auction_schedule_and_minimum_bid",
            "freshness_state": freshness_state,
            "freshness_basis": "official_upcoming_auction_date",
            "freshness_age_seconds": round(age, 3),
            "session_status": session_status,
            "auction_at": auction_at.isoformat(),
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Hacienda La Esmeralda public Esmeralda Auction FAQ",
            "source_url": source_url,
            "lot_catalogue_url": catalogue_url,
            "coffee_variety": "green-tip Geisha",
            "geisha_farms": farms,
            "harvest_month_labels": named_harvest_months,
            "harvest_month_mapping": harvest_months,
            "auction_phases": ("open_bidding", "timed_countdown"),
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
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
            "inst_id": f"HACIENDA_LA_ESMERALDA:{source_key.upper()}:HEALTH",
            "instrument_id": f"HACIENDA_LA_ESMERALDA:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": (
                "public_esmeralda_auction_parser_failure"
                if parser_error
                else "public_esmeralda_auction_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class HaciendaLaEsmeraldaEsmeraldaAuctionAdapter:
    info = AdapterInfo(
        adapter_id="hacienda_la_esmeralda_esmeralda_auction",
        venue=VENUE,
        market_type="green_coffee_microlot_auction_reference",
        source="Hacienda La Esmeralda public Esmeralda Auction FAQ and lot catalogue",
        capabilities=(
            "public_market_data",
            "auction_schedule",
            "minimum_bid_reference",
            "lot_catalogue_link",
            "harvest_month_taxonomy",
            "lot_story_reference",
            "source_health",
        ),
        aliases=(
            "hacienda la esmeralda",
            "esmeralda auction",
            "esmeralda geisha auction",
            "green-tip geisha",
            "panama geisha microlots",
        ),
        docs_url=FAQ_URL,
        runtime_entrypoint=(
            "adapters.venues.hacienda_la_esmeralda_esmeralda_auction."
            "HaciendaLaEsmeraldaEsmeraldaAuctionAdapter"
        ),
        quote_assets=("USD_PER_KG",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 30.0)))
        faq_url = str(cfg.get("faq_url") or FAQ_URL)
        auction_url = str(cfg.get("auction_url") or AUCTION_URL)
        faq_result = fetch_text(faq_url, timeout)
        catalogue_result = fetch_text(auction_url, timeout)
        fetch_status = {
            "faq": _fetch_evidence(faq_result, faq_url),
            "lot_catalogue": _fetch_evidence(catalogue_result, auction_url),
        }
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        if not faq_result.get("ok"):
            observations.append(_failure_observation("faq", faq_url, faq_result))
        else:
            try:
                observations.extend(
                    parse_hacienda_la_esmeralda_esmeralda_auction_faq(
                        str(faq_result.get("text") or ""),
                        source_url=faq_url,
                        catalogue_url=auction_url,
                        received_at=faq_result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
            except (HaciendaLaEsmeraldaParseError, TypeError, ValueError) as exc:
                message = f"Esmeralda Auction FAQ parser failed: {exc}"[:300]
                parser_failures.append({"source": "faq", "source_url": faq_url, "error": message})
                observations.append(_failure_observation("faq", faq_url, faq_result, message))
        if not catalogue_result.get("ok"):
            observations.append(_failure_observation("lot_catalogue", auction_url, catalogue_result))

        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if real_rows and all(status == "reachable" for status in statuses) and not parser_failures:
            source_status = "reachable"
        elif real_rows or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1318,
                "source_status": source_status,
                "source_url": faq_url,
                "source_urls": [faq_url, auction_url],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_lot_level_bid_history_clearing_prices_and_order_routing",
                "paper_only": True,
            },
        )


register_adapter(HaciendaLaEsmeraldaEsmeraldaAuctionAdapter())
