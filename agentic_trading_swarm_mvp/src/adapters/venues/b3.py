"""B3 official public-data-hub surface catalog adapter.

The hub entries describe public data and product catalogs; they are not
executable quotes.  Reachable entries are emitted as official observations,
but remain watch-only so neither paper nor live order routing can infer a
price from catalog availability.
"""

from __future__ import annotations

import unicodedata
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


HUB_URL = "https://www.b3.com.br/en_us/data/public-data-hub/"
PORTUGUESE_HUB_URL = "https://www.b3.com.br/pt_br/dados/hub-de-dados-publicos/"
DISTRIBUTOR_FAQ_URL = (
    "https://www.b3.com.br/en_us/market-data-and-indices/data-services/"
    "market-data/distributors/faq/"
)
MARKET_SURFACE = "b3_public_data_hub"


class B3PublicDataParseError(ValueError):
    """Raised when a reachable B3 hub no longer exposes the documented surfaces."""


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        attributes = dict(attrs)
        self._href = str(attributes.get("href") or "").strip()
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        label = " ".join("".join(self._parts).split())
        self.anchors.append((label, self._href))
        self._href = None
        self._parts = []


def _token(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    words = "".join(char if char.isalnum() else " " for char in ascii_text.lower())
    return " ".join(words.split())


def _contains_all(*terms: str) -> Callable[[str], bool]:
    return lambda label: all(term in label for term in terms)


SURFACES: tuple[dict[str, Any], ...] = (
    {
        "surface_id": "unsponsored_bdr",
        "name": "Unsponsored Brazilian Depositary Receipts (BDRs)",
        "market_type": "cash_equity_reference",
        "asset_class": "unsponsored_brazilian_depositary_receipt",
        "matches": lambda label: (
            "bdr" in label
            and ("unsponsored" in label or "nao patrocinad" in label)
            and "depositary receipt" in label
        ),
    },
    {
        "surface_id": "bdr_etf",
        "name": "Brazilian Depositary Receipts for ETFs",
        "market_type": "cash_equity_reference",
        "asset_class": "bdr_etf",
        "matches": _contains_all("bdr", "etf"),
    },
    {
        "surface_id": "stock_etf",
        "name": "Stock Exchange Traded Fund (Stock ETF)",
        "market_type": "cash_equity_reference",
        "asset_class": "stock_etf",
        "matches": lambda label: (
            "stock exchange traded fund" in label
            or "stock etf" in label
            or "etf de renda variavel" in label
        ),
    },
    {
        "surface_id": "fi_infra",
        "name": "Infrastructure Investment Funds (FI-Infra)",
        "market_type": "investment_fund_reference",
        "asset_class": "infrastructure_investment_fund",
        "matches": lambda label: "fi infra" in label or "fiinfra" in label,
    },
    {
        "surface_id": "fiagro",
        "name": "Agroindustrial Productive Chain Investment Funds (FIAGRO)",
        "market_type": "investment_fund_reference",
        "asset_class": "agroindustrial_investment_fund",
        "matches": lambda label: "fiagro" in label,
    },
    {
        "surface_id": "fidc",
        "name": "Credit Rights Investment Funds (FIDC)",
        "market_type": "investment_fund_reference",
        "asset_class": "credit_rights_investment_fund",
        "matches": lambda label: "fidc" in label.split(),
    },
    {
        "surface_id": "cbio",
        "name": "Decarbonization Credits (CBIO)",
        "market_type": "otc_environmental_reference",
        "asset_class": "decarbonization_credit",
        "matches": lambda label: "cbio" in label and "decarbon" in label,
    },
)


def parse_b3_public_data_hub(
    html: str,
    *,
    source_url: str = HUB_URL,
    received_at: str | None = None,
) -> list[dict]:
    """Normalize the seven documented B3 public-data-hub surface links."""

    if not isinstance(html, str) or not html.strip():
        raise B3PublicDataParseError("empty HTML response")
    parser = _AnchorParser()
    parser.feed(html)
    timestamp = received_at or utc_now()
    observations: list[dict] = []
    missing: list[str] = []

    for surface in SURFACES:
        match: tuple[str, str] | None = None
        for label, href in parser.anchors:
            if surface["matches"](_token(label)):
                match = (label, href)
                break
        if match is None:
            missing.append(str(surface["surface_id"]))
            continue
        label, href = match
        target_url = urljoin(source_url, href)
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            missing.append(str(surface["surface_id"]))
            continue
        surface_id = str(surface["surface_id"])
        inst_id = f"B3:PUBLIC_DATA_SURFACE:{surface_id.upper()}"
        observations.append(
            {
                "venue": "B3",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": surface_id.upper(),
                "name": str(surface["name"]),
                "source_label": label,
                "base": surface_id.upper(),
                "quote": "N/A",
                "market_type": str(surface["market_type"]),
                "market_surface": f"b3_{surface_id}_public_data",
                "asset_class": str(surface["asset_class"]),
                "trade_type": "official_market_catalog",
                "direction": "watch_only",
                "last": 0.0,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_public_data_surface",
                "freshness_state": "fresh",
                "freshness_basis": "official_catalog_fetch",
                "freshness_age_seconds": 0.0,
                "session_status": "reference_catalog",
                "observed_at": timestamp,
                "fetched_at": timestamp,
                "price_source": "B3 official public data hub",
                "source_url": target_url,
                "source_catalog_url": source_url,
                "candidate_reject_reason": "public_catalog_not_executable_quote",
            }
        )

    if missing:
        raise B3PublicDataParseError(
            "required B3 public-data surfaces were not found: " + ", ".join(missing)
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
    result: dict[str, Any],
    *,
    source_url: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("B3", source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_catalog_url": source_url,
            "candidate_reject_reason": (
                "public_catalog_parser_failure"
                if parser_error
                else "public_catalog_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class B3PublicDataHubAdapter:
    info = AdapterInfo(
        adapter_id="b3_public_data_hub",
        venue="B3",
        market_type="multi_asset_reference",
        source="B3 official public data hub",
        capabilities=(
            "public_market_data",
            "catalog",
            "public_data_catalog",
            "unsponsored_bdr",
            "bdr_etf",
            "stock_etf",
            "fi_infra",
            "fiagro",
            "fidc",
            "cbio",
            "source_health",
        ),
        aliases=(
            "b3",
            "brasil bolsa balcao",
            "brazil stock exchange",
            "brazilian depositary receipts",
            "fi-infra",
            "fiagro",
            "fidc",
            "cbio",
        ),
        docs_url=HUB_URL,
        runtime_entrypoint="adapters.venues.b3.B3PublicDataHubAdapter",
        quote_assets=(),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or HUB_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url=source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_b3_public_data_hub(
                    result.get("text") or "",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
                freshness_state = "fresh"
                session_state = "reference_catalog"
            except (B3PublicDataParseError, TypeError, ValueError) as exc:
                message = f"B3 public-data-hub parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [
                    _failure_observation(result, source_url=source_url, parser_error=message)
                ]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 622,
                "source_status": source_status,
                "source_urls": [HUB_URL, PORTUGUESE_HUB_URL, DISTRIBUTOR_FAQ_URL],
                "fetch_status": {"hub": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "surface_count": sum(
                    1
                    for row in observations
                    if row.get("quality_status") == "official_public_data_surface"
                ),
                "capability_gap": "public_entry_quality_quotes",
                "paper_only": True,
            },
        )


register_adapter(B3PublicDataHubAdapter())
