"""B3 official public-data-hub surface catalog adapter.

The hub entries describe public data and product catalogs; they are not
executable quotes. Reachable entries are emitted as official observations,
but remain watch-only so neither paper nor live order routing can infer a
price from catalog availability. The BDR ETF and CBIO surfaces are
additionally paired with defensible public companion quotes so Strategy Lab
can paper-test those reference surfaces without fabricating native B3 prices.
"""

from __future__ import annotations

import datetime as dt
import re
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
B3_BDR_ETF_COMPANION_QUOTE_SYMBOL = "EWZ"
B3_BDR_ETF_COMPANION_QUOTE_URL = "https://www.tradingview.com/symbols/AMEX-EWZ/"
B3_BDR_ETF_COMPANION_MARKET_SURFACE = "b3_bdr_etf_public_data"
B3_CBIO_COMPANION_QUOTE_SYMBOL = "KRBN"
B3_CBIO_COMPANION_QUOTE_URL = "https://www.tradingview.com/symbols/NYSEARCA-KRBN/"
B3_CBIO_COMPANION_MARKET_SURFACE = "b3_cbio_public_data"
COMPANION_QUOTE_SYMBOL = B3_BDR_ETF_COMPANION_QUOTE_SYMBOL
COMPANION_QUOTE_URL = B3_BDR_ETF_COMPANION_QUOTE_URL
COMPANION_MARKET_SURFACE = B3_BDR_ETF_COMPANION_MARKET_SURFACE
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


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise B3PublicDataParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


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
                "source_adapter_id": "b3_public_data_hub",
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


def parse_tradingview_b3_etf_quote(
    payload: str,
    *,
    symbol: str = B3_BDR_ETF_COMPANION_QUOTE_SYMBOL,
    source_url: str = B3_BDR_ETF_COMPANION_QUOTE_URL,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse a public Brazil ETF quote page used as a paper-only companion price."""

    return _parse_tradingview_companion_quote(
        payload,
        symbol=symbol,
        source_url=source_url,
        price_basis="public_companion_brazil_equity_etf_quote",
        proxy_symbol=f"AMEX:{str(symbol or '').strip().upper()}",
        price_reference_role="brazil_equity_etf_proxy",
        price_source="TradingView public Brazil ETF companion quote",
        error_prefix="TradingView Brazil ETF quote page",
        received_at=received_at,
    )


def parse_tradingview_cbio_companion_quote(
    payload: str,
    *,
    symbol: str = B3_CBIO_COMPANION_QUOTE_SYMBOL,
    source_url: str = B3_CBIO_COMPANION_QUOTE_URL,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse a public carbon ETF quote page used as a paper-only CBIO companion price."""

    return _parse_tradingview_companion_quote(
        payload,
        symbol=symbol,
        source_url=source_url,
        price_basis="public_companion_global_carbon_etf_quote",
        proxy_symbol=f"NYSEARCA:{str(symbol or '').strip().upper()}",
        price_reference_role="listed_carbon_market_proxy",
        price_source="TradingView public carbon ETF companion quote",
        error_prefix="TradingView CBIO companion quote page",
        received_at=received_at,
    )


def _parse_tradingview_companion_quote(
    payload: str,
    *,
    symbol: str,
    source_url: str,
    price_basis: str,
    proxy_symbol: str,
    price_reference_role: str,
    price_source: str,
    error_prefix: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse a public TradingView quote page used as a paper-only companion price."""

    text = str(payload or "").strip()
    quote_symbol = str(symbol or "").strip().upper()
    if not text:
        raise B3PublicDataParseError(f"{error_prefix} is empty")
    if not quote_symbol:
        raise B3PublicDataParseError(f"{error_prefix} symbol is missing")
    match = re.search(
        rf"The current price of {re.escape(quote_symbol)} is ([0-9]+(?:\.[0-9]+)?)\s*USD",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise B3PublicDataParseError(
            f"{error_prefix} missing current price for {quote_symbol}"
        )
    try:
        last = float(match.group(1))
    except ValueError as exc:
        raise B3PublicDataParseError(
            f"{error_prefix} has invalid current price for {quote_symbol}"
        ) from exc
    if last <= 0:
        raise B3PublicDataParseError(
            f"{error_prefix} current price must be positive for {quote_symbol}"
        )
    fetched_at = _received_time(received_at)
    return {
        "last": last,
        "price_available": True,
        "price_basis": price_basis,
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "proxy_symbol": proxy_symbol,
        "companion_quote_symbol": quote_symbol,
        "companion_quote_url": source_url,
        "freshness_state": "fresh",
        "freshness_basis": "public_quote_page_fetch",
        "freshness_age_seconds": 0.0,
        "session_status": "unknown",
        "session_basis": "public_quote_page_has_no_session_clock",
        "price_reference_role": price_reference_role,
        "price_source": price_source,
        "source_record_type": "tradingview_public_symbol_faq",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }


def _apply_companion_quote(observation: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Preserve B3 surface provenance while attaching a public companion quote."""

    updated = dict(observation)
    updated["last"] = float(quote["last"])
    updated["price_available"] = True
    updated["quote"] = "USD"
    updated["price_basis"] = str(quote["price_basis"])
    updated["quality_status"] = str(quote["quality_status"])
    updated["proxy_quality_status"] = str(quote["proxy_quality_status"])
    updated["proxy_symbol"] = str(quote["proxy_symbol"])
    updated["freshness_state"] = str(quote["freshness_state"])
    updated["freshness_basis"] = str(quote["freshness_basis"])
    updated["freshness_age_seconds"] = float(quote["freshness_age_seconds"])
    updated["session_status"] = str(quote["session_status"])
    updated["session_basis"] = str(quote["session_basis"])
    updated["price_reference_role"] = str(quote["price_reference_role"])
    updated["price_source"] = str(quote["price_source"])
    updated["source_record_type"] = str(quote["source_record_type"])
    updated["source_contract_url"] = str(updated.get("source_url") or "")
    updated["source_url"] = str(quote["companion_quote_url"])
    updated["companion_quote_symbol"] = str(quote["companion_quote_symbol"])
    updated["companion_quote_url"] = str(quote["companion_quote_url"])
    updated["observed_at"] = str(quote["observed_at"])
    updated["fetched_at"] = str(quote["fetched_at"])
    updated["candidate_reject_reason"] = "public_companion_price_requires_strategy_logic"
    return updated


def _apply_bdr_etf_companion_quote(observation: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Preserve B3 surface provenance while attaching a public Brazil ETF proxy price."""

    return _apply_companion_quote(observation, quote)


def _apply_cbio_companion_quote(observation: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Preserve B3 CBIO provenance while attaching a public carbon ETF proxy price."""

    return _apply_companion_quote(observation, quote)


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
            "source_adapter_id": "b3_public_data_hub",
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
        companion_source_url = str(cfg.get("companion_quote_url") or B3_BDR_ETF_COMPANION_QUOTE_URL)
        companion_symbol = str(cfg.get("companion_quote_symbol") or B3_BDR_ETF_COMPANION_QUOTE_SYMBOL)
        cbio_companion_source_url = str(
            cfg.get("cbio_companion_quote_url") or B3_CBIO_COMPANION_QUOTE_URL
        )
        cbio_companion_symbol = str(
            cfg.get("cbio_companion_quote_symbol") or B3_CBIO_COMPANION_QUOTE_SYMBOL
        )
        result = fetch_text(source_url, timeout)
        companion_result: dict[str, Any] | None = None
        cbio_companion_result: dict[str, Any] | None = None
        parser_failures: list[dict[str, str]] = []
        companion_failures: list[dict[str, str]] = []

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
                companion_result = fetch_text(companion_source_url, timeout)
                if companion_result.get("ok"):
                    try:
                        companion_quote = parse_tradingview_b3_etf_quote(
                            companion_result.get("text") or "",
                            symbol=companion_symbol,
                            source_url=companion_source_url,
                            received_at=companion_result.get("received_at"),
                        )
                        observations = [
                            _apply_bdr_etf_companion_quote(row, companion_quote)
                            if row.get("market_surface") == B3_BDR_ETF_COMPANION_MARKET_SURFACE
                            else row
                            for row in observations
                        ]
                    except (B3PublicDataParseError, TypeError, ValueError) as exc:
                        companion_failures.append(
                            {
                                "source_url": companion_source_url,
                                "error": f"B3 companion quote parser failed: {exc}"[:300],
                            }
                        )
                else:
                    companion_failures.append(
                        {
                            "source_url": companion_source_url,
                            "error": str(companion_result.get("error") or "companion quote unavailable")[:300],
                        }
                    )
                cbio_companion_result = fetch_text(cbio_companion_source_url, timeout)
                if cbio_companion_result.get("ok"):
                    try:
                        cbio_companion_quote = parse_tradingview_cbio_companion_quote(
                            cbio_companion_result.get("text") or "",
                            symbol=cbio_companion_symbol,
                            source_url=cbio_companion_source_url,
                            received_at=cbio_companion_result.get("received_at"),
                        )
                        observations = [
                            _apply_cbio_companion_quote(row, cbio_companion_quote)
                            if row.get("market_surface") == B3_CBIO_COMPANION_MARKET_SURFACE
                            else row
                            for row in observations
                        ]
                    except (B3PublicDataParseError, TypeError, ValueError) as exc:
                        companion_failures.append(
                            {
                                "source_url": cbio_companion_source_url,
                                "error": f"B3 CBIO companion quote parser failed: {exc}"[:300],
                            }
                        )
                else:
                    companion_failures.append(
                        {
                            "source_url": cbio_companion_source_url,
                            "error": str(
                                cbio_companion_result.get("error") or "CBIO companion quote unavailable"
                            )[:300],
                        }
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
                "source_urls": [
                    HUB_URL,
                    PORTUGUESE_HUB_URL,
                    DISTRIBUTOR_FAQ_URL,
                    B3_BDR_ETF_COMPANION_QUOTE_URL,
                    B3_CBIO_COMPANION_QUOTE_URL,
                ],
                "fetch_status": {
                    "hub": _fetch_evidence(result, source_url),
                    "bdr_etf_companion": (
                        _fetch_evidence(companion_result, companion_source_url)
                        if companion_result is not None
                        else {
                            "source_url": companion_source_url,
                            "fetch_status": "not_attempted",
                            "http_status": None,
                            "fetched_at": None,
                            "latency_ms": None,
                            "error": None,
                        }
                    ),
                    "cbio_companion": (
                        _fetch_evidence(cbio_companion_result, cbio_companion_source_url)
                        if cbio_companion_result is not None
                        else {
                            "source_url": cbio_companion_source_url,
                            "fetch_status": "not_attempted",
                            "http_status": None,
                            "fetched_at": None,
                            "latency_ms": None,
                            "error": None,
                        }
                    ),
                },
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "companion_failures": companion_failures,
                "observation_count": len(observations),
                "surface_count": sum(
                    1
                    for row in observations
                    if row.get("quality_status") == "official_public_data_surface"
                ),
                "priceable_surface_count": sum(1 for row in observations if float(row.get("last") or 0.0) > 0.0),
                "capability_gap": "public_entry_quality_quotes",
                "paper_only": True,
            },
        )


register_adapter(B3PublicDataHubAdapter())
