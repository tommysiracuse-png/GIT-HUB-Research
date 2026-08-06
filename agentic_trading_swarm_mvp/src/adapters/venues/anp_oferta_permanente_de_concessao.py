"""ANP Oferta Permanente de Concessao (OPC) public exploration-block adapter.

ANP publishes the OPC catalogue and amendment notices without credentials.  The
material describes exploration opportunities and regulatory-event timing, not
an executable security or an oil price.  This adapter consequently emits only
watch-only, paper-research reference observations.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


BLOCKS_URL = "https://www.gov.br/anp/pt-br/rodadas-anp/oferta-permanente/opc/blocos-exploratorios"
ANNOUNCEMENT_URL = (
    "https://www.gov.br/anp/pt-br/canais_atendimento/imprensa/noticias-comunicados/"
    "oferta-permanente-de-concessao-opc-edital-com-inclusao-de-45-blocos-passara-por-audiencia-publica"
)
DASHBOARD_URL = (
    "https://www.gov.br/anp/pt-br/centrais-de-conteudo/paineis-dinamicos-da-anp/"
    "paineis-dinamicos-sobre-exploracao-e-producao-de-petroleo-e-gas/"
    "painel-dinamico-da-oferta-permanente"
)
COMPANION_QUOTE_SYMBOL = "PBR"
COMPANION_QUOTE_URL = "https://www.tradingview.com/symbols/NYSE-PBR/"
MARKET_SURFACE = "anp_oferta_permanente_de_concessao"
VENUE = "ANP_BRAZIL_OPC"


class AnpOfertaPermanenteParseError(ValueError):
    """Raised when a reachable ANP page no longer has the documented fields."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _plain_text(payload: str) -> str:
    if not isinstance(payload, str) or not payload.strip():
        raise AnpOfertaPermanenteParseError("response must be non-empty HTML text")
    parser = _TextExtractor()
    try:
        parser.feed(payload)
    except ValueError as exc:
        raise AnpOfertaPermanenteParseError("invalid HTML response") from exc
    text = parser.text()
    if not text:
        raise AnpOfertaPermanenteParseError("response has no visible text")
    return text


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnpOfertaPermanenteParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _portuguese_date(value: str) -> dt.date:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", value)
    if not match:
        raise AnpOfertaPermanenteParseError(f"invalid Portuguese publication date: {value!r}")
    try:
        return dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError as exc:
        raise AnpOfertaPermanenteParseError(f"invalid Portuguese publication date: {value!r}") from exc


def _date_after_label(text: str, label: str) -> dt.date | None:
    match = re.search(
        label + r"\s*(?:em\s*)?(\d{1,2}/\d{1,2}/20\d{2})", _normalized(text), re.I
    )
    return _portuguese_date(match.group(1)) if match else None


def _freshness(
    source_date: dt.date | None,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> tuple[str, float, str]:
    if source_date is None:
        return "fresh", 0.0, "official_page_fetch"
    published_at = dt.datetime.combine(source_date, dt.time.min, tzinfo=dt.timezone.utc)
    age = max(0.0, (fetched_at - published_at).total_seconds())
    state = "fresh" if age <= max(0.0, stale_after_days) * 86400 else "stale"
    return state, round(age, 3), "official_publication_date"


def _reference_row(
    *,
    inst_id: str,
    symbol: str,
    name: str,
    market_type: str,
    source_url: str,
    fetched_at: dt.datetime,
    freshness_state: str,
    freshness_age_seconds: float,
    freshness_basis: str,
    session_status: str,
    quality_status: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": "BRAZIL_EXPLORATION_BLOCKS",
        "quote": "N/A",
        "market_type": market_type,
        "market_surface": MARKET_SURFACE,
        "asset_class": "oil_and_gas_exploration_rights_reference",
        "trade_type": "official_regulatory_programme_reference",
        "direction": "watch_only",
        "last": 0.0,
        "price_available": False,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": quality_status,
        "freshness_state": freshness_state,
        "freshness_basis": freshness_basis,
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Agencia Nacional do Petroleo, Gas Natural e Biocombustiveis public OPC record",
        "source_url": source_url,
        "paper_route_status": "synthetic_research_only",
        "candidate_reject_reason": "official_exploration_block_reference_not_executable_quote",
        **details,
    }


def parse_anp_opc_exploratory_blocks(
    payload: str,
    *,
    source_url: str = BLOCKS_URL,
    received_at: str | None = None,
    stale_after_days: float = 45.0,
) -> list[dict[str, Any]]:
    """Normalize ANP's currently available OPC exploration-block catalogue."""

    text = _plain_text(payload)
    normalized = _normalized(text)
    if "blocos exploratorios" not in normalized or "oferta permanente" not in normalized:
        raise AnpOfertaPermanenteParseError("OPC exploratory-block catalogue markers were not found")
    count_match = re.search(
        r"(?:total\s+de\s+)?([\d.]+)\s+blocos\s+exploratorios\s+disponiveis",
        normalized,
    )
    if not count_match:
        raise AnpOfertaPermanenteParseError("available exploratory-block count was not found")
    available_blocks = int(count_match.group(1).replace(".", ""))
    fetched_at = _received_time(received_at)
    updated_at = _date_after_label(text, r"Atualizado")
    freshness_state, age, basis = _freshness(updated_at, fetched_at, stale_after_days)
    return [
        _reference_row(
            inst_id="ANP:OPC:EXPLORATORY_BLOCKS:AVAILABLE",
            symbol="OPC_EXPLORATORY_BLOCKS",
            name="ANP Oferta Permanente de Concessao available exploratory blocks",
            market_type="exploration_block_catalog",
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age,
            freshness_basis=basis,
            session_status="available_for_continuous_offer",
            quality_status="official_exploration_block_catalog",
            available_exploratory_blocks=available_blocks,
            catalogue_updated_date=updated_at.isoformat() if updated_at else None,
        )
    ]


def parse_anp_opc_45_block_announcement(
    payload: str,
    *,
    source_url: str = ANNOUNCEMENT_URL,
    received_at: str | None = None,
    stale_after_days: float = 120.0,
) -> list[dict[str, Any]]:
    """Normalize the official April 2026 OPC amendment notice for 45 blocks."""

    text = _plain_text(payload)
    normalized = _normalized(text)
    if "oferta permanente de concessao" not in normalized or "novos blocos exploratorios" not in normalized:
        raise AnpOfertaPermanenteParseError("OPC 45-block announcement markers were not found")
    total_match = re.search(r"inclusao\s+de\s+(\d+)\s+novos\s+blocos\s+exploratorios", normalized)
    offshore_match = re.search(r"(\d+)\s+maritimos", normalized)
    onshore_match = re.search(r"(\d+|oito)\s+terrestres", normalized)
    if not total_match or not offshore_match or not onshore_match:
        raise AnpOfertaPermanenteParseError("OPC announcement block-count breakdown was not found")
    total_blocks = int(total_match.group(1))
    offshore_blocks = int(offshore_match.group(1))
    onshore_blocks = 8 if onshore_match.group(1) == "oito" else int(onshore_match.group(1))
    if total_blocks != offshore_blocks + onshore_blocks:
        raise AnpOfertaPermanenteParseError("OPC announcement block-count breakdown does not reconcile")
    required_basins = ("campos", "santos", "potiguar")
    if not all(basin in normalized for basin in required_basins):
        raise AnpOfertaPermanenteParseError("OPC announcement basin markers were not found")
    published_at = _date_after_label(text, r"Publicado")
    if published_at is None:
        raise AnpOfertaPermanenteParseError("OPC announcement publication date was not found")
    fetched_at = _received_time(received_at)
    freshness_state, age, basis = _freshness(published_at, fetched_at, stale_after_days)
    public_hearing = _date_after_label(text, r"audiencia publica no dia")
    return [
        _reference_row(
            inst_id=f"ANP:OPC:NEW_EXPLORATORY_BLOCKS:{published_at.isoformat()}",
            symbol="OPC_45_NEW_BLOCKS",
            name="ANP OPC announcement adding 45 exploratory blocks",
            market_type="exploration_block_programme_amendment",
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age,
            freshness_basis=basis,
            session_status="public_consultation_announced",
            quality_status="official_exploration_block_amendment_notice",
            new_exploratory_blocks=total_blocks,
            offshore_new_blocks=offshore_blocks,
            onshore_new_blocks=onshore_blocks,
            offshore_basins=("Campos", "Santos"),
            onshore_basins=("Potiguar",),
            announcement_published_date=published_at.isoformat(),
            public_hearing_date=public_hearing.isoformat() if public_hearing else None,
        )
    ]


def parse_tradingview_petrobras_adr_quote(
    payload: str,
    *,
    symbol: str = COMPANION_QUOTE_SYMBOL,
    source_url: str = COMPANION_QUOTE_URL,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse a public Petrobras ADR quote page used as a paper-only companion price."""

    text = str(payload or "").strip()
    quote_symbol = str(symbol or "").strip().upper()
    if not text:
        raise AnpOfertaPermanenteParseError("TradingView Petrobras ADR quote page is empty")
    if not quote_symbol:
        raise AnpOfertaPermanenteParseError("TradingView Petrobras ADR quote symbol is missing")
    match = re.search(
        rf"The current price of {re.escape(quote_symbol)} is ([0-9]+(?:\.[0-9]+)?)\s*USD",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise AnpOfertaPermanenteParseError(
            f"TradingView Petrobras ADR quote page missing current price for {quote_symbol}"
        )
    try:
        last = float(match.group(1))
    except ValueError as exc:
        raise AnpOfertaPermanenteParseError(
            f"TradingView Petrobras ADR quote page has invalid current price for {quote_symbol}"
        ) from exc
    if last <= 0:
        raise AnpOfertaPermanenteParseError(
            f"TradingView Petrobras ADR quote page current price must be positive for {quote_symbol}"
        )
    fetched_at = _received_time(received_at)
    return {
        "last": last,
        "price_available": True,
        "price_basis": "public_companion_petrobras_adr_quote",
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "proxy_symbol": f"NYSE:{quote_symbol}",
        "companion_quote_symbol": quote_symbol,
        "companion_quote_url": source_url,
        "price_reference_role": "brazil_upstream_equity_proxy",
        "price_source": "TradingView public Petrobras ADR companion quote",
        "source_record_type": "tradingview_public_symbol_faq",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }


def _apply_companion_quote(observation: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Preserve the ANP regulatory record while attaching a public Petrobras proxy price."""

    updated = dict(observation)
    updated["last"] = float(quote["last"])
    updated["price_available"] = True
    updated["quote"] = "USD_PER_ADR_PROXY"
    updated["price_basis"] = str(quote["price_basis"])
    updated["quality_status"] = str(quote["quality_status"])
    updated["proxy_quality_status"] = str(quote["proxy_quality_status"])
    updated["proxy_symbol"] = str(quote["proxy_symbol"])
    updated["price_reference_role"] = str(quote["price_reference_role"])
    updated["price_source"] = str(quote["price_source"])
    updated["source_record_type"] = str(quote["source_record_type"])
    updated["source_programme_url"] = str(updated.get("source_url") or "")
    updated["source_url"] = str(quote["companion_quote_url"])
    updated["companion_quote_symbol"] = str(quote["companion_quote_symbol"])
    updated["companion_quote_url"] = str(quote["companion_quote_url"])
    updated["companion_observed_at"] = str(quote["observed_at"])
    updated["companion_fetched_at"] = str(quote["fetched_at"])
    updated["candidate_reject_reason"] = "public_companion_price_requires_strategy_logic"
    return updated


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
            "inst_id": f"ANP:OPC:{source_key.upper()}:HEALTH",
            "instrument_id": f"ANP:OPC:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "BRAZIL_EXPLORATION_BLOCKS",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "candidate_reject_reason": (
                "public_anp_opc_parser_failure" if parser_error else "public_anp_opc_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class AnpOfertaPermanenteDeConcessaoAdapter:
    info = AdapterInfo(
        adapter_id="anp_oferta_permanente_de_concessao",
        venue=VENUE,
        market_type="exploration_block_programme_reference",
        source="ANP Brazil Oferta Permanente de Concessao public exploration-block records",
        capabilities=(
            "public_market_data",
            "exploration_block_catalog",
            "exploration_block_amendment_notice",
            "public_consultation_schedule",
            "basin_breakdown",
            "source_health",
        ),
        aliases=(
            "anp",
            "brazil anp",
            "oferta permanente de concessao",
            "oferta permanente",
            "opc",
            "brazil exploration blocks",
            "campos santos potiguar",
        ),
        docs_url=BLOCKS_URL,
        runtime_entrypoint=(
            "adapters.venues.anp_oferta_permanente_de_concessao."
            "AnpOfertaPermanenteDeConcessaoAdapter"
        ),
        quote_assets=(),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        companion_quote_url = str(cfg.get("companion_quote_url") or COMPANION_QUOTE_URL)
        sources = (
            (
                "blocks_catalog",
                str(cfg.get("blocks_url") or BLOCKS_URL),
                parse_anp_opc_exploratory_blocks,
                max(0.0, float(cfg.get("catalog_stale_after_days", 45.0))),
            ),
            (
                "45_block_announcement",
                str(cfg.get("announcement_url") or ANNOUNCEMENT_URL),
                parse_anp_opc_45_block_announcement,
                max(0.0, float(cfg.get("announcement_stale_after_days", 120.0))),
            ),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        companion_fetch_status: dict[str, dict[str, Any]] = {}
        companion_failures: list[dict[str, str]] = []
        usable_sources = 0
        for source_key, source_url, parser, stale_after_days in sources:
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                observations.extend(
                    parser(
                        str(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
                usable_sources += 1
            except (AnpOfertaPermanenteParseError, TypeError, ValueError) as exc:
                message = f"ANP OPC {source_key} parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source": source_key, "source_url": source_url, "error": message}
                )
                observations.append(_failure_observation(source_key, source_url, result, message))

        real_rows = [row for row in observations if row.get("quality_status", "") != "source_health"]
        if real_rows:
            result = fetch_text(companion_quote_url, timeout)
            companion_fetch_status[COMPANION_QUOTE_SYMBOL] = _fetch_evidence(result, companion_quote_url)
            if not result.get("ok"):
                companion_failures.append(
                    {
                        "symbol": COMPANION_QUOTE_SYMBOL,
                        "source_url": companion_quote_url,
                        "error": str(result.get("error") or "public companion quote unavailable")[:300],
                    }
                )
            else:
                try:
                    quote = parse_tradingview_petrobras_adr_quote(
                        str(result.get("text") or ""),
                        symbol=COMPANION_QUOTE_SYMBOL,
                        source_url=companion_quote_url,
                        received_at=result.get("received_at"),
                    )
                    enriched_observations: list[dict[str, Any]] = []
                    for row in observations:
                        if row.get("quality_status") == "source_health":
                            enriched_observations.append(row)
                        else:
                            enriched_observations.append(_apply_companion_quote(row, quote))
                    observations = enriched_observations
                except (AnpOfertaPermanenteParseError, TypeError, ValueError) as exc:
                    companion_failures.append(
                        {
                            "symbol": COMPANION_QUOTE_SYMBOL,
                            "source_url": companion_quote_url,
                            "error": f"ANP OPC Petrobras companion quote parser failed: {exc}"[:300],
                        }
                    )

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(sources) and not parser_failures and not companion_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures or companion_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        real_rows = [row for row in observations if row.get("quality_status", "") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1247,
                "source_status": source_status,
                "source_url": sources[0][1],
                "source_urls": [sources[0][1], sources[1][1], DASHBOARD_URL, companion_quote_url],
                "fetch_status": fetch_status,
                "companion_fetch_status": companion_fetch_status,
                "freshness_state": (
                    freshness_states[0]
                    if len(freshness_states) == 1
                    else "mixed"
                    if freshness_states
                    else "unknown"
                ),
                "freshness_states": freshness_states,
                "session_state": (
                    session_states[0]
                    if len(session_states) == 1
                    else "mixed"
                    if session_states
                    else "unknown"
                ),
                "session_states": session_states,
                "parser_failures": parser_failures,
                "companion_failures": companion_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "entry_quality_prices_bid_interest_and_order_routing",
                "paper_only": True,
            },
        )


register_adapter(AnpOfertaPermanenteDeConcessaoAdapter())
