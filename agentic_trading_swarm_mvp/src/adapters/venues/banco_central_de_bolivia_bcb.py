"""Banco Central de Bolivia (BCB) OMA and public repo-auction references.

BCB publishes its weekly Open Market Operations (OMA) result sheets and the
terms for public repo operations without authentication.  These are official
primary-market references, rather than executable quotes, so every emitted
row remains watch-only and can only support synthetic paper experiments.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


OMA_PUBLICATION_URL = (
    "https://www.bcb.gob.bo/?q=content%2Fdifusi%C3%B3n-de-resultados-de-la-subasta-72026-publicaci%C3%B3n-oma"
)
REPO_RULES_URL = "https://www.bcb.gob.bo/?q=content%2Fciex-n%C2%B0-72025"
MARKET_SURFACE = "bcb_oma_273_day_values_and_public_repo_auctions"
VENUE = "BANCO_CENTRAL_DE_BOLIVIA"
_LA_PAZ_TIME = dt.timezone(dt.timedelta(hours=-4), name="America/La_Paz")


class BancoCentralDeBoliviaParseError(ValueError):
    """Raised when a reachable BCB publication loses required OMA facts."""


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
        raise BancoCentralDeBoliviaParseError("official response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - source drift is retained as health evidence.
        raise BancoCentralDeBoliviaParseError(f"invalid HTML response: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).split())
    if not text:
        raise BancoCentralDeBoliviaParseError("official response has no visible text")
    return text


def extract_pdf_text(body: bytes) -> str:
    """Extract text from BCB's public OMA result PDF."""

    if not isinstance(body, bytes) or not body:
        raise BancoCentralDeBoliviaParseError("OMA result PDF is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared in requirements-autonomous.txt.
        raise BancoCentralDeBoliviaParseError("pypdf is required to read BCB OMA result PDFs") from exc
    try:
        text = "\n".join(str(page.extract_text() or "") for page in PdfReader(io.BytesIO(body)).pages)
    except Exception as exc:  # noqa: BLE001 - public PDFs may change without notice.
        raise BancoCentralDeBoliviaParseError(f"OMA result PDF could not be read: {exc}") from exc
    if not text.strip():
        raise BancoCentralDeBoliviaParseError("OMA result PDF contains no extractable text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BancoCentralDeBoliviaParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _decimal(value: str, field: str, *, grouped_thousands: bool = False) -> float:
    text = str(value or "").strip().replace(" ", "")
    if grouped_thousands:
        text = text.replace(".", "").replace(",", "")
    elif "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError as exc:
        raise BancoCentralDeBoliviaParseError(f"invalid {field}: {value!r}") from exc
    if number < 0:
        raise BancoCentralDeBoliviaParseError(f"{field} must not be negative")
    return number


def _publication_date(text: str) -> dt.date:
    match = re.search(
        r"Adjudicaciones\s+de\s+Subasta\s+de\s+la\s+semana\s+\d+\s*/\s*20\d{2}\s*,?\s*del\s+(\d{1,2}/\d{1,2}/20\d{2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise BancoCentralDeBoliviaParseError("OMA auction-result date was not found")
    try:
        return dt.datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError as exc:
        raise BancoCentralDeBoliviaParseError("OMA auction-result date is invalid") from exc


def _auction_week(text: str) -> str | None:
    match = re.search(r"Subasta\s+de\s+la\s+semana\s+(\d+\s*/\s*20\d{2})", text, re.I)
    return match.group(1).replace(" ", "") if match else None


def parse_bcb_oma_result_links(document: str, *, source_url: str = OMA_PUBLICATION_URL) -> list[str]:
    """Extract BCB's OMA result-PDF link from the official publication page."""

    visible = _visible_text(document)
    if "subasta" not in visible.casefold() or "oma" not in visible.casefold():
        raise BancoCentralDeBoliviaParseError("OMA publication-page markers were not found")
    links: list[str] = []
    for match in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a\s*>', document, re.I | re.S):
        label = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(match.group(2))).split())
        if "subasta" not in label.casefold() and "oma" not in label.casefold():
            continue
        link = urljoin(source_url, html.unescape(match.group(1)).strip())
        if link not in links:
            links.append(link)
    if not links:
        raise BancoCentralDeBoliviaParseError("OMA publication page did not contain a result PDF link")
    return links


def _freshness(auction_date: dt.date, fetched_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    auction_at = dt.datetime.combine(auction_date, dt.time.min, tzinfo=_LA_PAZ_TIME)
    age = max(0.0, (fetched_at - auction_at.astimezone(dt.timezone.utc)).total_seconds())
    return ("fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale", round(age, 3))


def _oma_observation(
    *,
    security: str,
    term_days: int,
    protection_days: int | None,
    awarded_quantity_thousands: float,
    awarded_rate_pct: float,
    auction_date: dt.date,
    auction_week: str | None,
    source_url: str,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> dict[str, Any]:
    freshness_state, age_seconds = _freshness(auction_date, fetched_at, stale_after_days)
    symbol = f"{security.replace('-', '_')}_{term_days}D"
    inst_id = f"{VENUE}:{symbol}:AUCTION:{auction_date.isoformat()}"
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": f"BCB {security} {term_days}-day OMA auction result",
        "base": symbol,
        "quote": "BOB_RATE_PCT",
        "market_type": "central_bank_open_market_auction_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": "central_bank_bill",
        "trade_type": "official_oma_auction_result",
        "direction": "watch_only",
        "last": awarded_rate_pct,
        "auction_date": auction_date.isoformat(),
        "auction_week": auction_week,
        "auction_term_days": term_days,
        "security_type": security,
        "protection_period_days": protection_days,
        "auction_awarded_rate_pct": awarded_rate_pct,
        "auction_weighted_average_rate_pct": awarded_rate_pct,
        "auction_awarded_quantity_thousands": awarded_quantity_thousands,
        "auction_awarded_nominal_bob": awarded_quantity_thousands * 1_000.0,
        "amount_unit": "BOB nominal; BCB sheet quantity is in thousands",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_oma_auction_result",
        "freshness_state": freshness_state,
        "freshness_basis": "official_oma_auction_date",
        "freshness_age_seconds": age_seconds,
        "session_status": "weekly_auction_results_published",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Banco Central de Bolivia official OMA result sheet",
        "source_url": source_url,
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": True,
    }


def _repo_observation(
    *,
    minimum_rate_pct: float,
    maximum_rate_pct: float,
    maximum_amount_bob: float,
    term_days: int,
    auction_date: dt.date,
    source_url: str,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> dict[str, Any]:
    freshness_state, age_seconds = _freshness(auction_date, fetched_at, stale_after_days)
    inst_id = f"{VENUE}:REPO_MN:OFFER:{auction_date.isoformat()}"
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": "BCB_REPO_MN",
        "name": "BCB public auction repo offer in bolivianos",
        "base": "BCB_REPO_MN",
        "quote": "BOB_RATE_PCT",
        "market_type": "central_bank_repo_auction_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": "central_bank_repo",
        "trade_type": "official_public_repo_offer",
        "direction": "watch_only",
        "last": maximum_rate_pct,
        "repo_currency": "BOB",
        "repo_term_days": term_days,
        "repo_minimum_rate_pct": minimum_rate_pct,
        "repo_maximum_rate_pct": maximum_rate_pct,
        "repo_offer_maximum_amount_bob": maximum_amount_bob,
        "auction_date": auction_date.isoformat(),
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_public_repo_offer",
        "freshness_state": freshness_state,
        "freshness_basis": "official_oma_auction_date",
        "freshness_age_seconds": age_seconds,
        "session_status": "public_repo_offer_published",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Banco Central de Bolivia official OMA result sheet",
        "source_url": source_url,
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": True,
    }


def parse_bcb_oma_auction_results(
    document: str,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 21.0,
) -> list[dict[str, Any]]:
    """Normalize BCB award rates/volumes and public repo-offer terms from OMA sheets."""

    if not isinstance(document, str) or not document.strip():
        raise BancoCentralDeBoliviaParseError("OMA result document is empty")
    text = "\n".join(line.strip() for line in document.replace("\r", "\n").splitlines() if line.strip())
    if "Adjudicaciones de Subasta" not in text or "Oferta de Reportos" not in text:
        raise BancoCentralDeBoliviaParseError("OMA result document markers were not found")
    auction_date = _publication_date(text)
    fetched_at = _received_time(received_at)
    rows: list[dict[str, Any]] = []
    row_pattern = re.compile(
        r"(?im)^\s*(L[BR]-MN)\s+BCB\s+(\d{1,4})\s+(-|\d+)\s+([\d.]+)\s+(\d+(?:,\d+)?)\s*$"
    )
    seen_awards: set[tuple[str, int]] = set()
    for match in row_pattern.finditer(text):
        security, term, protection, quantity, rate = match.groups()
        term_days = int(term)
        if not 1 <= term_days <= 3_000:
            continue
        award_key = (security, term_days)
        # BCB sheets render the awarded and next-week offered tables before
        # their section labels in extracted PDF text.  The first occurrence is
        # the adjudication table; later duplicates are the offer schedule.
        if award_key in seen_awards:
            continue
        seen_awards.add(award_key)
        rows.append(
            _oma_observation(
                security=security,
                term_days=term_days,
                protection_days=None if protection == "-" else int(protection),
                awarded_quantity_thousands=_decimal(quantity, "awarded quantity", grouped_thousands=True),
                awarded_rate_pct=_decimal(rate, "awarded rate"),
                auction_date=auction_date,
                auction_week=_auction_week(text),
                source_url=source_url,
                fetched_at=fetched_at,
                stale_after_days=stale_after_days,
            )
        )
    if not rows or not any(row["auction_term_days"] == 273 for row in rows):
        raise BancoCentralDeBoliviaParseError("OMA result did not contain the required 273-day BCB award")

    repo_match = re.search(
        r"\bMN\s+(\d+(?:[,.]\d+)?)\s+(\d+(?:[,.]\d+)?)\s+Bs\s*([\d.]+)\s+(\d+)\s+Diario",
        text,
        re.IGNORECASE,
    )
    if not repo_match:
        raise BancoCentralDeBoliviaParseError("public repo-offer terms were not found")
    minimum, maximum, amount, term = repo_match.groups()
    rows.append(
        _repo_observation(
            minimum_rate_pct=_decimal(minimum, "repo minimum rate"),
            maximum_rate_pct=_decimal(maximum, "repo maximum rate"),
            maximum_amount_bob=_decimal(amount, "repo maximum amount", grouped_thousands=True),
            term_days=int(term),
            auction_date=auction_date,
            source_url=source_url,
            fetched_at=fetched_at,
            stale_after_days=stale_after_days,
        )
    )
    return sorted(rows, key=lambda row: str(row["inst_id"]))


def parse_bcb_electronic_auction_and_repo_rules(
    document: str,
    *,
    source_url: str = REPO_RULES_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Record the official public-auction and repo-system rule notice."""

    text = _visible_text(document)
    required = ("operaciones de mercado abierto", "sistema electrónico", "subasta electrónica de valores y reportos")
    missing = [marker for marker in required if marker not in text.casefold()]
    if missing:
        raise BancoCentralDeBoliviaParseError("electronic-auction rules missing markers: " + ", ".join(missing))
    fetched_at = _received_time(received_at)
    number_match = re.search(r"CIEX\s*N[°ºo.]?\s*(\d+)\s*/\s*(20\d{2})", text, re.I)
    notice = f"CIEX_{number_match.group(1)}_{number_match.group(2)}" if number_match else "CIEX_ELECTRONIC_AUCTION_RULES"
    inst_id = f"{VENUE}:{notice}"
    return [
        {
            "venue": VENUE,
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": notice,
            "name": "BCB electronic public values-and-repo auction participation rules",
            "base": notice,
            "quote": "N/A",
            "market_type": "central_bank_auction_operational_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "auction_operational_rules",
            "trade_type": "official_public_auction_rules",
            "direction": "watch_only",
            "last": 0.0,
            "electronic_auction_enabled": True,
            "public_repo_operations_enabled": True,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_electronic_auction_and_repo_rules",
            "freshness_state": "reference",
            "freshness_basis": "official_rules_page_fetch_timestamp",
            "freshness_age_seconds": 0.0,
            "session_status": "electronic_public_auction_rules_published",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Banco Central de Bolivia CIEX public notice",
            "source_url": source_url,
            "paper_route": "synthetic_reference",
            "execution_mode": "paper_only",
            "paper_experiment_eligible": False,
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
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "paper_route": "synthetic_reference",
            "execution_mode": "paper_only",
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class BancoCentralDeBoliviaAdapter:
    info = AdapterInfo(
        adapter_id="banco_central_de_bolivia_bcb_oma",
        venue=VENUE,
        market_type="central_bank_open_market_auction_reference",
        source="Banco Central de Bolivia public OMA and electronic repo-auction publications",
        capabilities=(
            "public_market_data",
            "auction_results",
            "auction_yield",
            "award_size",
            "public_repo_auction",
            "event_price_reference",
            "source_health",
        ),
        aliases=(
            "banco central de bolivia",
            "bcb",
            "bcb oma",
            "operaciones de mercado abierto",
            "subasta de valores",
            "operaciones de reporto",
        ),
        docs_url=OMA_PUBLICATION_URL,
        runtime_entrypoint="adapters.venues.banco_central_de_bolivia_bcb.BancoCentralDeBoliviaAdapter",
        quote_assets=("BOB_RATE_PCT", "BOB"),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 21.0)))
        result_limit = max(1, min(int(cfg.get("max_result_documents", 1)), 3))
        publication_url = str(cfg.get("oma_publication_url") or OMA_PUBLICATION_URL)
        rules_url = str(cfg.get("repo_rules_url") or REPO_RULES_URL)
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0
        result_links: list[str] = []

        publication = fetch_text(publication_url, timeout)
        fetch_status["oma_publication"] = _fetch_evidence(publication, publication_url)
        if not publication.get("ok"):
            observations.append(_failure_observation("oma_publication", publication_url, publication))
        else:
            try:
                result_links = parse_bcb_oma_result_links(str(publication.get("text") or ""), source_url=publication_url)[:result_limit]
                usable_sources += 1
            except (BancoCentralDeBoliviaParseError, TypeError, ValueError) as exc:
                message = f"BCB OMA publication parser failed: {exc}"[:300]
                parser_failures.append({"source_key": "oma_publication", "source_url": publication_url, "error": message})
                observations.append(_failure_observation("oma_publication", publication_url, publication, message))

        for sequence, source_url in enumerate(result_links, start=1):
            source_key = f"oma_result_{sequence}"
            result = fetch_bytes(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                document = str(result["text"]) if result.get("text") else extract_pdf_text(result.get("content") or b"")
                observations.extend(
                    parse_bcb_oma_auction_results(
                        document,
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
                usable_sources += 1
            except (BancoCentralDeBoliviaParseError, TypeError, ValueError) as exc:
                message = f"BCB OMA result parser failed: {exc}"[:300]
                parser_failures.append({"source_key": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        rules = fetch_text(rules_url, timeout)
        fetch_status["repo_rules"] = _fetch_evidence(rules, rules_url)
        if not rules.get("ok"):
            observations.append(_failure_observation("repo_rules", rules_url, rules))
        else:
            try:
                observations.extend(
                    parse_bcb_electronic_auction_and_repo_rules(
                        str(rules.get("text") or ""), source_url=rules_url, received_at=rules.get("received_at")
                    )
                )
                usable_sources += 1
            except (BancoCentralDeBoliviaParseError, TypeError, ValueError) as exc:
                message = f"BCB repo-rules parser failed: {exc}"[:300]
                parser_failures.append({"source_key": "repo_rules", "source_url": rules_url, "error": message})
                observations.append(_failure_observation("repo_rules", rules_url, rules, message))

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(fetch_status) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        real_rows = [row for row in observations if str(row.get("quality_status") or "").startswith("official_")]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 690,
                "source_status": source_status,
                "source_url": publication_url,
                "source_urls": [publication_url, rules_url, *result_links],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "result_document_count": len(result_links),
                "capability_gap": "public_secondary_market_BOB_quotes_and_executable_order_routing_not_available",
                "paper_only": True,
            },
        )


BancoCentralDeBoliviaBcbAdapter = BancoCentralDeBoliviaAdapter
register_adapter(BancoCentralDeBoliviaAdapter())
