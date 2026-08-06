"""Public Bappebti registry and auction-reference adapter for Apkarkusi.

Bappebti publicly lists Koperasi Assosiasi Petani Karet Kuantan Singingi
(Apkarkusi) as a registered Pasar Lelang Komoditas organiser in Riau.  The
registry does not expose an order entry endpoint.  It is nevertheless useful
paper evidence: it confirms the venue, and the parser also retains any
published BOKAR auction price table that Bappebti places on the public market
page.  All observations are watch-only and explicitly have no live route.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, utc_now
from scan_batch import ScanBatch


ORGANIZER_URL = "https://bappebti.go.id/pasar_lelang/daftar_penyelenggara"
MARKET_URL = "https://bappebti.go.id/pasar_lelang"
OVERSIGHT_REPORT_URL = (
    "https://bappebti.go.id/resources/docs/"
    "L3Laporan%20Triwulan%20II%20Biro%20Pengawasan%20PBK%2C%20SRG%2C%20dan%20PLK%20Tahun%202024.pdf"
)
DEVELOPMENT_REPORT_URL = (
    "https://bappebti.go.id/resources/docs/"
    "L3BIRO%20PEMBINAAN%20DAN%20PENGEMBANGAN%20SRG%20DAN%20PLK%20%281%29.pdf"
)
VENUE = "BAPPEBTI_APKARKUSI"
MARKET_SURFACE = "bappebti_apkarkusi_karet_bokar_auction"
ORGANIZER_NAME = "Koperasi Assosiasi Petani Karet Kuantan Singingi"
APPROVAL_NUMBER = "01/Bappebti/Kep-PL/SP/07/2020"


class BappebtiApkarkusiParseError(ValueError):
    """Raised when an otherwise reachable Bappebti page loses venue markers."""


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
        raise BappebtiApkarkusiParseError("Bappebti response must be non-empty HTML text")
    parser = _VisibleTextParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain a visible source-health record on layout changes.
        raise BappebtiApkarkusiParseError(f"invalid Bappebti HTML: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).split())
    if not text:
        raise BappebtiApkarkusiParseError("Bappebti response has no visible text")
    return text


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BappebtiApkarkusiParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _source_row(text: str, payload: str) -> str:
    """Return the organiser's table row where one is available."""

    for table in html_tables(payload):
        for row in table:
            joined = " ".join(row)
            if "assosiasi petani karet kuantan singingi" in joined.casefold():
                return joined
    start = text.casefold().find("koperasi assosiasi petani karet kuantan singingi")
    return text[start : start + 1_000] if start >= 0 else text


def _approval_number(text: str) -> str:
    compact = re.sub(r"\s*/\s*", "/", text)
    match = re.search(r"\b\d{1,3}/bappebti/kep-pl/sp/\d{2}/20\d{2}\b", compact, re.IGNORECASE)
    if not match:
        raise BappebtiApkarkusiParseError("Bappebti approval number was not found")
    return match.group(0)


def parse_bappebti_apkarkusi_organizer_listing(
    payload: str,
    *,
    source_url: str = ORGANIZER_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the official Bappebti Apkarkusi organiser registration.

    This registry record is a real public observation, but not a price.  It
    deliberately remains separate from price observations so that the scanner
    never invents a BOKAR price where the public page only confirms a venue.
    """

    text = _visible_text(payload)
    lowered = text.casefold()
    if "koperasi assosiasi petani karet kuantan singingi" not in lowered:
        raise BappebtiApkarkusiParseError("Apkarkusi organiser marker was not found")
    row = _source_row(text, payload)
    approval = _approval_number(row if "bappebti" in row.casefold() else text)
    if "riau" not in row.casefold() and "riau" not in lowered:
        raise BappebtiApkarkusiParseError("Apkarkusi Riau location marker was not found")
    fetched_at = _received_at(received_at)
    return [
        {
            "venue": VENUE,
            "inst_id": f"{VENUE}:ORGANIZER_REGISTRATION",
            "instrument_id": f"{VENUE}:ORGANIZER_REGISTRATION",
            "symbol": "APKARKUSI_REGISTRATION",
            "name": ORGANIZER_NAME,
            "base": "INDONESIAN_BOKAR_NATURAL_RUBBER",
            "quote": "IDR_PER_KG",
            "market_type": "natural_rubber_auction_registry_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "natural_rubber_bokar",
            "trade_type": "official_auction_organizer_registration",
            "direction": "watch_only",
            "last": 0.0,
            "price_available": False,
            "price_basis": "registration_only_no_published_auction_price",
            "data_access_type": "public_no_key",
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_registered_auction_organizer",
            "freshness_state": "fresh",
            "freshness_basis": "official_registry_fetch_time",
            "freshness_age_seconds": 0.0,
            "session_status": "market_session_unknown",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Bappebti public Pasar Lelang Komoditas organiser registry",
            "source_url": source_url,
            "organizer_name": ORGANIZER_NAME,
            "organizer_alias": "Apkarkusi",
            "bappebti_approval_number": approval,
            "location": "Kuantan Singingi, Riau, Indonesia",
            "commodity": "karet bokar",
            "auction_frequency": "weekly_when_public_session_data_is_published",
            "participant_count_available": False,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
        }
    ]


def _indonesian_number(value: str) -> float | None:
    text = str(value or "").strip().replace("\u00a0", " ")
    text = re.sub(r"(?i)rp\.?|idr|/\s*kg|kg|orang|peserta|lot", "", text)
    text = re.sub(r"[^0-9,.-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.search(r"\.\d{3}(?:$|\.)", text):
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _session_date(value: str) -> dt.date | None:
    value = " ".join(str(value or "").split())
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _header_index(headers: list[str], *terms: str) -> int | None:
    for index, header in enumerate(headers):
        lowered = header.casefold()
        if any(term in lowered for term in terms):
            return index
    return None


def parse_bappebti_apkarkusi_auction_prices(
    payload: str,
    *,
    source_url: str = MARKET_URL,
    received_at: str | None = None,
    stale_after_days: float = 14.0,
) -> list[dict[str, Any]]:
    """Normalize published BOKAR session rows when the public page supplies them.

    Tables must identify a session date and a price column.  Participant and
    lot fields are optional diagnostics, never eligibility gates for a paper
    observation.
    """

    fetched_at = _received_at(received_at)
    observations: list[dict[str, Any]] = []
    for table in html_tables(payload):
        if len(table) < 2:
            continue
        headers = [str(cell).casefold() for cell in table[0]]
        date_index = _header_index(headers, "tanggal", "date", "sesi", "session")
        price_index = _header_index(headers, "harga", "price")
        commodity_index = _header_index(headers, "komoditas", "commodity", "barang")
        participant_index = _header_index(headers, "peserta", "participant", "pembeli", "buyer")
        lot_index = _header_index(headers, "lot", "volume", "kuantitas", "quantity", "berat")
        if date_index is None or price_index is None:
            continue
        for sequence, cells in enumerate(table[1:], start=1):
            if max(date_index, price_index) >= len(cells):
                continue
            commodity = cells[commodity_index] if commodity_index is not None and commodity_index < len(cells) else "BOKAR"
            table_text = " ".join(cells).casefold()
            if "karet" not in commodity.casefold() and "bokar" not in commodity.casefold() and "karet" not in table_text and "bokar" not in table_text:
                continue
            session_date = _session_date(cells[date_index])
            price = _indonesian_number(cells[price_index])
            if not session_date or price is None or price <= 0:
                continue
            age_seconds = max(0.0, (fetched_at.date() - session_date).total_seconds())
            participant_count = (
                _indonesian_number(cells[participant_index])
                if participant_index is not None and participant_index < len(cells)
                else None
            )
            lot_volume_kg = (
                _indonesian_number(cells[lot_index]) if lot_index is not None and lot_index < len(cells) else None
            )
            observations.append(
                {
                    "venue": VENUE,
                    "inst_id": f"{VENUE}:BOKAR:{session_date.isoformat()}:{sequence}",
                    "instrument_id": f"{VENUE}:BOKAR:{session_date.isoformat()}:{sequence}",
                    "symbol": "APKARKUSI_BOKAR",
                    "name": f"Apkarkusi BOKAR auction {session_date.isoformat()}",
                    "base": "INDONESIAN_BOKAR_NATURAL_RUBBER",
                    "quote": "IDR_PER_KG",
                    "market_type": "natural_rubber_auction_lot_price",
                    "market_surface": MARKET_SURFACE,
                    "asset_class": "natural_rubber_bokar",
                    "trade_type": "official_public_auction_price",
                    "direction": "watch_only",
                    "last": price,
                    "price_available": True,
                    "price_basis": "published_bokar_auction_price_idr_per_kg",
                    "published_price_idr_per_kg": price,
                    "commodity": commodity,
                    "participant_count": int(participant_count) if participant_count and participant_count.is_integer() else participant_count,
                    "lot_volume_kg": lot_volume_kg,
                    "data_access_type": "public_no_key",
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_public_auction_price",
                    "freshness_state": "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400 else "stale",
                    "freshness_basis": "published_auction_session_date",
                    "freshness_age_seconds": round(age_seconds, 3),
                    "session_status": "completed",
                    "auction_session_date": session_date.isoformat(),
                    "observed_at": fetched_at.isoformat(),
                    "fetched_at": fetched_at.isoformat(),
                    "price_source": "Bappebti public Pasar Lelang Komoditas page",
                    "source_url": source_url,
                    "organizer_name": ORGANIZER_NAME,
                    "paper_route_status": "synthetic_research_only",
                    "execution_route_status": "route_needed",
                }
            )
    return observations


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
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "INDONESIAN_BOKAR_NATURAL_RUBBER",
            "quote": "IDR_PER_KG",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": (
                "public_bappebti_apkarkusi_parser_failure"
                if parser_error
                else "public_bappebti_apkarkusi_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class BappebtiApkarkusiAdapter:
    info = AdapterInfo(
        adapter_id="bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi",
        venue=VENUE,
        market_type="natural_rubber_auction_lot_price",
        source="Bappebti public Pasar Lelang Komoditas registry and market page",
        capabilities=(
            "public_market_data",
            "registered_auction_organizer",
            "natural_rubber_bokar_reference",
            "published_auction_price_when_available",
            "participant_count_when_available",
            "source_health",
        ),
        aliases=(
            "bappebti apkarkusi",
            "koperasi apkarkusi",
            "kuantan singingi rubber auction",
            "riau bokar auction",
            "karet bokar",
        ),
        docs_url=ORGANIZER_URL,
        runtime_entrypoint=(
            "adapters.venues.bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi."
            "BappebtiApkarkusiAdapter"
        ),
        quote_assets=("IDR_PER_KG",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 14.0)))
        organizer_url = str(cfg.get("organizer_url") or ORGANIZER_URL)
        market_url = str(cfg.get("market_url") or MARKET_URL)
        organizer_result = fetch_text(organizer_url, timeout)
        market_result = fetch_text(market_url, timeout)
        results = {"organizer_registry": (organizer_url, organizer_result), "market_page": (market_url, market_result)}
        fetch_status = {key: _fetch_evidence(result, url) for key, (url, result) in results.items()}
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        if not organizer_result.get("ok"):
            observations.append(_failure_observation("organizer_registry", organizer_url, organizer_result))
        else:
            try:
                observations.extend(
                    parse_bappebti_apkarkusi_organizer_listing(
                        str(organizer_result.get("text") or ""),
                        source_url=organizer_url,
                        received_at=organizer_result.get("received_at"),
                    )
                )
            except (BappebtiApkarkusiParseError, TypeError, ValueError) as exc:
                message = f"Bappebti Apkarkusi registry parser failed: {exc}"[:300]
                parser_failures.append({"source": "organizer_registry", "source_url": organizer_url, "error": message})
                observations.append(_failure_observation("organizer_registry", organizer_url, organizer_result, message))

        if not market_result.get("ok"):
            observations.append(_failure_observation("market_page", market_url, market_result))
        else:
            try:
                observations.extend(
                    parse_bappebti_apkarkusi_auction_prices(
                        str(market_result.get("text") or ""),
                        source_url=market_url,
                        received_at=market_result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
            except (BappebtiApkarkusiParseError, TypeError, ValueError) as exc:
                message = f"Bappebti Apkarkusi auction-price parser failed: {exc}"[:300]
                parser_failures.append({"source": "market_page", "source_url": market_url, "error": message})
                observations.append(_failure_observation("market_page", market_url, market_result, message))

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
                "adapter_spec_id": 1378,
                "source_status": source_status,
                "source_url": organizer_url,
                "source_urls": [organizer_url, market_url, OVERSIGHT_REPORT_URL, DEVELOPMENT_REPORT_URL],
                "fetch_status": fetch_status,
                "freshness_state": freshness[0] if len(freshness) == 1 else ("mixed" if freshness else "unknown"),
                "session_state": sessions[0] if len(sessions) == 1 else ("mixed" if sessions else "unknown"),
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_weekly_lot_price_history_and_execution_routing_not_guaranteed",
                "paper_only": True,
            },
        )


register_adapter(BappebtiApkarkusiAdapter())
