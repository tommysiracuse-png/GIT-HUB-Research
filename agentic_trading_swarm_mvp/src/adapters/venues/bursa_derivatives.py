"""Public Bursa Malaysia Derivatives Berhad contract-catalog adapter.

The exchange's rules schedule is public and keyless, but it is a contract
reference rather than a quote or order-book feed.  Parsed contracts are useful
for cross-contract research only and are deliberately never order-routable.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import zlib
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, health_observation, utc_now
from scan_batch import ScanBatch


RULES_URL = (
    "https://www.bursamalaysia.com/sites/5d809dcf39fba22790cad230/assets/"
    "669e3630e6414a82930d1c84/"
    "Consolidated_Rules_of_Bursa_Malaysia_Derivatives_Bhd_18March24_updated.pdf"
)
SOURCE_URL = RULES_URL
MARKET_SURFACE = "bursa_malaysia_derivatives_contract_catalog"

# The symbols and contract descriptions are the exchange's published product
# identifiers.  These fields are identity metadata, not a claim of a current
# listed expiry, price, settlement price, or executable market.
CONTRACTS = (
    ("FCPO", "Crude Palm Oil Futures Contract", "commodity_futures", "Crude palm oil"),
    (
        "FKLI",
        "FTSE Bursa Malaysia KLCI Futures Contract",
        "equity_index_futures",
        "FTSE Bursa Malaysia KLCI",
    ),
    (
        "OKLI",
        "Option on FTSE Bursa Malaysia KLCI Futures",
        "equity_index_options",
        "FTSE Bursa Malaysia KLCI Futures",
    ),
    (
        "FM70",
        "Mini FTSE Bursa Malaysia Mid 70 Index Futures Contract",
        "equity_index_futures",
        "FTSE Bursa Malaysia Mid 70 Index",
    ),
    (
        "F4GM",
        "FTSE4Good Bursa Malaysia Index Futures Contract",
        "equity_index_futures",
        "FTSE4Good Bursa Malaysia Index",
    ),
    ("FMG5", "Mini Gold Futures Contract", "commodity_futures", "Gold"),
)


class BursaDerivativesParseError(ValueError):
    """Raised when a reachable official rules document has no usable catalog."""


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
        raise BursaDerivativesParseError("official Bursa derivatives response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - report changed upstream documents.
        raise BursaDerivativesParseError(f"invalid official Bursa HTML: {exc}") from exc
    text = html.unescape(" ".join(parser.parts))
    # Plain text/PDF extraction fixtures do not necessarily contain HTML tags.
    return " ".join(text.replace("\x00", " ").split())


def _pdf_stream_text(content: bytes) -> str:
    """Extract readable fragments from simple Flate-compressed public PDFs.

    This intentionally has no third-party PDF dependency.  It is a best-effort
    supplement to the literal text already present in a document; a source
    whose font encoding cannot be read is recorded as a parser failure instead
    of being guessed.
    """

    fragments = [content.decode("latin-1", errors="ignore")]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        try:
            decoded = zlib.decompress(match.group(1))
        except zlib.error:
            continue
        fragments.append(decoded.decode("latin-1", errors="ignore"))
    return " ".join(fragments)


def _document_text(document: str | bytes) -> str:
    if isinstance(document, bytes):
        document = _pdf_stream_text(document)
    return _visible_text(document)


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BursaDerivativesParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _contract_row(
    symbol: str,
    name: str,
    asset_class: str,
    underlying: str,
    *,
    source_url: str,
    fetched_at: dt.datetime,
) -> dict[str, Any]:
    market_type = "options_catalog" if symbol == "OKLI" else "futures_catalog"
    return {
        "venue": "BURSA_MALAYSIA_DERIVATIVES",
        "inst_id": f"BURSA_MALAYSIA_DERIVATIVES:{symbol}",
        "instrument_id": f"BURSA_MALAYSIA_DERIVATIVES:{symbol}",
        "symbol": symbol,
        "name": name,
        "base": symbol,
        "quote": "MYR",
        "market_type": market_type,
        "market_surface": MARKET_SURFACE,
        "asset_class": asset_class,
        "trade_type": "official_contract_catalog",
        "direction": "watch_only",
        "last": 0.0,
        "price_basis": "contract_catalog_only",
        "underlying": underlying,
        "settlement_reference": "Bursa Malaysia Derivatives contract rules",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_contract_catalog",
        "freshness_state": "fresh",
        "freshness_basis": "official_rules_document_fetch",
        "freshness_age_seconds": 0.0,
        "session_status": "reference_only",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Bursa Malaysia Derivatives Berhad published rules",
        "source_url": source_url,
        "candidate_reject_reason": "public_contract_catalog_not_executable_quote",
    }


def parse_bursa_derivatives_contract_catalog(
    document: str | bytes,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Bursa's official rules schedule into contract observations.

    A rules schedule is authoritative contract identity evidence, but does not
    establish that an expiry is currently listed or tradable.  Consequently all
    normalized rows remain watch-only with a zero price.
    """

    text = _document_text(document)
    normalized = re.sub(r"\s+", " ", text).upper()
    if not re.search(r"BURSA\s+MALAYSIA\s+DERIVATIVES", normalized):
        raise BursaDerivativesParseError("document is not a Bursa Malaysia Derivatives rules source")
    # All requested surface codes must be present.  This prevents a generic
    # Bursa announcement from being mistaken for a complete contract schedule.
    missing = [symbol for symbol, *_details in CONTRACTS if symbol not in normalized]
    if missing:
        raise BursaDerivativesParseError(
            "official rules schedule is missing contract code(s): " + ", ".join(missing)
        )
    fetched_at = _received_at(received_at)
    return [
        _contract_row(*contract, source_url=source_url, fetched_at=fetched_at)
        for contract in CONTRACTS
    ]


# Friendly aliases for callers that refer to this venue rather than the catalog.
parse_bursa_derivatives = parse_bursa_derivatives_contract_catalog
parse_bursa_malaysia_derivatives = parse_bursa_derivatives_contract_catalog


def contract_observations(source_status: str, source_url: str = SOURCE_URL) -> list[dict[str, Any]]:
    """Compatibility helper for catalog-only callers.

    Runtime code does not use this fallback: it emits only health evidence on a
    failed fetch.  Keeping the helper makes static callers explicit about their
    non-price, watch-only nature.
    """

    fetched_at = _received_at(utc_now())
    rows = [_contract_row(*contract, source_url=source_url, fetched_at=fetched_at) for contract in CONTRACTS]
    for row in rows:
        row.update(
            {
                "data_status": source_status,
                "fetch_status": source_status,
                "freshness_state": "fresh" if source_status == "reachable" else "unknown",
                "freshness_basis": "official_rules_document_fetch" if source_status == "reachable" else "source_unavailable",
                "freshness_age_seconds": 0.0 if source_status == "reachable" else None,
                "candidate_reject_reason": "public_quote_endpoint_not_available",
            }
        )
    return rows


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
    row = health_observation("BURSA_MALAYSIA_DERIVATIVES", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_bursa_derivatives_parser_failure"
                if parser_error
                else "public_bursa_derivatives_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class BursaMalaysiaDerivativesBerhadAdapter:
    info = AdapterInfo(
        adapter_id="bursa_derivatives_contract_catalog",
        venue="BURSA_MALAYSIA_DERIVATIVES",
        market_type="derivatives_contract_catalog",
        source="Bursa Malaysia Derivatives Berhad published contract rules",
        capabilities=(
            "public_market_data",
            "contract_identity",
            "futures",
            "options",
            "commodity_derivatives",
            "equity_index_derivatives",
            "settlement_reference",
            "source_health",
        ),
        aliases=(
            "bursa malaysia derivatives berhad",
            "bursa malaysia derivatives",
            "bursa derivatives",
            "bmd",
            "fcpo",
            "fkli",
            "okli",
            "fm70",
            "f4gm",
            "fmg5",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.bursa_derivatives.BursaMalaysiaDerivativesBerhadAdapter"
        ),
        quote_assets=("MYR",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_bytes(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_bursa_derivatives_contract_catalog(
                    result.get("content") or b"",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
                freshness_state = "fresh"
                session_state = "reference_only"
            except (BursaDerivativesParseError, TypeError, ValueError) as exc:
                message = f"Bursa Malaysia Derivatives parser failed: {exc}"[:300]
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
                "adapter_spec_id": 455,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url],
                "fetch_status": {"rules": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_quotes_and_order_book",
                "paper_only": True,
            },
        )


# Retain the previous generated class name for integrations already referencing it.
BursaDerivativesAdapter = BursaMalaysiaDerivativesBerhadAdapter


register_adapter(BursaMalaysiaDerivativesBerhadAdapter())
