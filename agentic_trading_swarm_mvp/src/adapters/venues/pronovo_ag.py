"""Pronovo AG public Swiss BT guarantees-of-origin references.

Pronovo operates Switzerland's public guarantees-of-origin registry for
renewable thermal and motor fuels (BT system). The public site documents the
registry scope, gas-certificate import routes, and hydrogen certificate
requirements, but it does not expose anonymous executable bids, offers, or
clearing prices. This adapter therefore emits watch-only, paper-only
observations for registry and import-reference research.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, slug, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://pronovo.ch/de/herkunftsnachweise/erneuerbare-treib-und-brennstoffe-bt/"
IMPORT_GAS_URL = "https://pronovo.ch/import-von-gas-hkn/"
HYDROGEN_REQUIREMENTS_URL = (
    "https://pronovo.ch/ufaqs/"
    "welche-anforderungen-gelten-fuer-h2-zertifikate-ist-eine-anrechnung-moeglich/"
)
LIQUID_NEWS_URL = (
    "https://pronovo.ch/news/"
    "schweizer-pionierarbeit-fuer-das-herkunftsnachweiswesen-fluessiger-brenn-und-treibstoffe/"
)
VENUE = "PRONOVO_AG"
MARKET_SURFACE = "swiss_bt_guarantees_of_origin"


class PronovoAgParseError(ValueError):
    """Raised when a public Pronovo page no longer matches the documented surface."""


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


def _ascii_fold(value: Any) -> str:
    return (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def _visible_text(document: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - upstream HTML changes must remain health evidence.
        raise PronovoAgParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _normalized_text(document: str) -> str:
    text = _ascii_fold(_visible_text(document))
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    return " ".join(text.split()).lower()


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PronovoAgParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _section(source: str, start_pattern: str, end_pattern: str | None = None) -> str:
    lower = source.lower()
    start = re.search(start_pattern, lower, re.IGNORECASE)
    if not start:
        raise PronovoAgParseError(f"section marker was not found: {start_pattern}")
    start_index = start.end()
    end_index = len(source)
    if end_pattern:
        end = re.search(end_pattern, lower[start_index:], re.IGNORECASE)
        if end:
            end_index = start_index + end.start()
    return source[start_index:end_index].strip()


def _registry_entries(section: str) -> list[str]:
    entries = []
    for country, registry in re.findall(
        r"([A-Za-z][A-Za-z .'\-]{1,80}?)\s*\(([^()]{2,80})\)",
        _ascii_fold(section),
    ):
        item = f"{country.strip()} ({registry.strip()})"
        if item not in entries:
            entries.append(item)
    return entries


def _base_observation(
    *,
    inst_id: str,
    symbol: str,
    name: str,
    base: str,
    market_type: str,
    trade_type: str,
    quality_status: str,
    session_status: str,
    source_url: str,
    fetched_at: dt.datetime,
) -> dict[str, Any]:
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": base,
        "quote": "HKN_BT_UNIT",
        "market_type": market_type,
        "market_surface": MARKET_SURFACE,
        "asset_class": "renewable_fuel_certificate",
        "trade_type": trade_type,
        "direction": "watch_only",
        "last": 0.0,
        "jurisdiction": "Switzerland",
        "registry_operator": "Pronovo AG",
        "registry_system": "SHKN_BT",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": quality_status,
        "freshness_state": "fresh",
        "freshness_basis": "official_pronovo_page_fetch_timestamp",
        "freshness_age_seconds": 0.0,
        "session_status": session_status,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "source_url": source_url,
    }


def parse_pronovo_bt_overview(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Pronovo's public BT system overview into registry-scope rows."""

    if not isinstance(document, str) or not document.strip():
        raise PronovoAgParseError("Pronovo BT overview response is empty")
    text = _normalized_text(document)
    markers = {
        "bt_system": r"herkunftsnachweise.*erneuerbare.*brenn.*treibstoffe",
        "mandatory_tracking": r"seit dem 1\. januar 2025.*gesetzliche pflicht",
        "domestic_import_scope": r"schweizerische produktion sowie der import",
        "gas_liquid_scope": r"gasf(?:o|oe)rmige und fl(?:u|ue)ssige brenn.*treibstoffe",
    }
    missing = [name for name, pattern in markers.items() if not re.search(pattern, text)]
    if missing:
        raise PronovoAgParseError("Pronovo BT overview markers were not found: " + ", ".join(missing))

    fetched_at = _received_time(received_at)
    gas = _base_observation(
        inst_id=f"{VENUE}:BT:REGISTRY:RENEWABLE_GAS",
        symbol="CH_BT_RENEWABLE_GAS_HKN",
        name="Swiss BT guarantees of origin for renewable gaseous fuels",
        base="RENEWABLE_GAS",
        market_type="renewable_fuel_guarantee_of_origin_reference",
        trade_type="official_registry_certificate_reference",
        quality_status="official_registry_reference",
        session_status="registry_reference",
        source_url=source_url,
        fetched_at=fetched_at,
    )
    gas.update(
        {
            "fuel_state": "gaseous",
            "fuel_family": "renewable_gas",
            "domestic_production_tracking_required": True,
            "import_tracking_required": True,
            "application_scope": ["motor_fuel", "heating_fuel"],
            "candidate_reject_reason": "official_registry_page_has_no_public_clearing_price",
            "supporting_source_urls": [IMPORT_GAS_URL, HYDROGEN_REQUIREMENTS_URL],
        }
    )

    liquid = _base_observation(
        inst_id=f"{VENUE}:BT:REGISTRY:LIQUID_BIOFUEL",
        symbol="CH_BT_LIQUID_BIOFUEL_HKN",
        name="Swiss BT guarantees of origin for renewable liquid fuels",
        base="LIQUID_BIOFUEL",
        market_type="renewable_fuel_guarantee_of_origin_reference",
        trade_type="official_registry_certificate_reference",
        quality_status="official_registry_reference",
        session_status="registry_reference",
        source_url=source_url,
        fetched_at=fetched_at,
    )
    liquid.update(
        {
            "fuel_state": "liquid",
            "fuel_family": "liquid_biofuel",
            "domestic_production_tracking_required": True,
            "import_tracking_required": True,
            "application_scope": ["motor_fuel", "heating_fuel"],
            "coverage_note": "Swiss BT system covers renewable liquid energy carriers.",
            "candidate_reject_reason": "official_registry_page_has_no_public_clearing_price",
            "supporting_source_urls": [LIQUID_NEWS_URL],
        }
    )
    return [gas, liquid]


def parse_pronovo_gas_import_routes(
    document: str,
    *,
    source_url: str = IMPORT_GAS_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Pronovo's public gas-certificate import route page."""

    if not isinstance(document, str) or not document.strip():
        raise PronovoAgParseError("Pronovo gas import response is empty")
    visible = _visible_text(document)
    text = " ".join(_ascii_fold(visible).split()).lower()
    markers = {
        "ergar": r"european renewable gas registry|ergar",
        "aib": r"association of issuing bodies|aib",
        "aib_hub": r"import von eecs-zertifikaten (?:uber|ueber) den aib-hub",
        "ergar_hub": r"import von coo (?:uber|ueber) den ergar-hub",
    }
    missing = [name for name, pattern in markers.items() if not re.search(pattern, text)]
    if missing:
        raise PronovoAgParseError("Pronovo gas import markers were not found: " + ", ".join(missing))

    aib_section = _section(
        visible,
        r"import von eecs-zertifikaten (?:uber|ueber) den aib-hub",
        r"import von coo (?:uber|ueber) den ergar-hub",
    )
    ergar_section = _section(visible, r"import von coo (?:uber|ueber) den ergar-hub")
    aib_entries = _registry_entries(aib_section)
    ergar_entries = _registry_entries(ergar_section)
    if not aib_entries or not ergar_entries:
        raise PronovoAgParseError("Pronovo gas import page has no usable AIB or ERGaR route entries")

    fetched_at = _received_time(received_at)
    rows = []
    for code, symbol, name, hub, scheme, entries in (
        (
            "AIB_HUB",
            "CH_BT_GAS_IMPORT_AIB",
            "Swiss BT import route for gas guarantees via AIB Hub",
            "AIB",
            "EECS",
            aib_entries,
        ),
        (
            "ERGAR_HUB",
            "CH_BT_GAS_IMPORT_ERGAR",
            "Swiss BT import route for gas guarantees via ERGaR Hub",
            "ERGaR",
            "CoO",
            ergar_entries,
        ),
    ):
        row = _base_observation(
            inst_id=f"{VENUE}:BT:GAS_IMPORT:{code}",
            symbol=symbol,
            name=name,
            base="RENEWABLE_GAS",
            market_type="renewable_gas_certificate_import_route_reference",
            trade_type="official_import_route_reference",
            quality_status="official_import_route_reference",
            session_status="import_route_reference",
            source_url=source_url,
            fetched_at=fetched_at,
        )
        row.update(
            {
                "fuel_state": "gaseous",
                "certificate_scheme": scheme,
                "import_hub": hub,
                "route_status": "available",
                "connected_jurisdictions": entries,
                "connected_jurisdiction_count": len(entries),
                "candidate_reject_reason": "official_import_route_has_no_public_clearing_price",
            }
        )
        rows.append(row)
    return rows


def parse_pronovo_hydrogen_requirements(
    document: str,
    *,
    source_url: str = HYDROGEN_REQUIREMENTS_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Pronovo's public H2 certificate eligibility guidance."""

    if not isinstance(document, str) or not document.strip():
        raise PronovoAgParseError("Pronovo hydrogen requirements response is empty")
    text = _normalized_text(document)
    markers = {
        "hydrogen": r"h2-zertifikate|wasserstoff",
        "foreign_gas": r"ausl(?:a|ae)ndischen zertifikaten.*erneuerbare gase",
        "grid_injection": r"ins europ(?:a|ae)ische gasnetz eingespeist",
        "positive_list": r"positivliste",
    }
    missing = [name for name, pattern in markers.items() if not re.search(pattern, text)]
    if missing:
        raise PronovoAgParseError(
            "Pronovo hydrogen requirements markers were not found: " + ", ".join(missing)
        )

    fetched_at = _received_time(received_at)
    positive_list_status = (
        "pending_publication"
        if re.search(r"liste ist noch nicht publiziert|wird bis ende jahr veroffentlicht", text)
        else "referenced"
    )
    row = _base_observation(
        inst_id=f"{VENUE}:BT:H2_IMPORT:REQUIREMENTS",
        symbol="CH_BT_H2_IMPORT_REQUIREMENTS",
        name="Swiss BT hydrogen certificate import requirements",
        base="RENEWABLE_HYDROGEN",
        market_type="renewable_hydrogen_certificate_import_requirement_reference",
        trade_type="official_import_eligibility_reference",
        quality_status="official_import_requirement_reference",
        session_status="import_requirement_reference",
        source_url=source_url,
        fetched_at=fetched_at,
    )
    row.update(
        {
            "fuel_state": "gaseous",
            "certificate_scope": "renewable_hydrogen_related_imports",
            "eligible_only_if_injected_into_european_gas_grid": True,
            "ecological_requirements_reference": "VHBT",
            "positive_list_status": positive_list_status,
            "physical_import_rule_reference": "USG_35D" if "art. 35d" in text else None,
            "candidate_reject_reason": "official_hydrogen_requirements_have_no_public_clearing_price",
        }
    )
    return [row]


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
    source_url: str,
    source_key: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:BT:{slug(source_key)}:HEALTH",
            "instrument_id": f"{VENUE}:BT:{slug(source_key)}:HEALTH",
            "symbol": f"BT_{slug(source_key)}_HEALTH",
            "base": "BT_SYSTEM_HEALTH",
            "quote": "N/A",
            "market_type": "renewable_fuel_guarantee_of_origin_reference",
            "source_key": source_key,
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_pronovo_bt_parser_failure"
                if parser_error
                else "public_pronovo_bt_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {
        **((root.get("adapters") or {}).get(adapter_id) or {}),
        **(root.get(adapter_id) or {}),
    }


def _summarize_states(rows: list[dict[str, Any]], field: str) -> tuple[str, list[str]]:
    values = sorted({str(row.get(field) or "unknown") for row in rows}) if rows else ["unknown"]
    if len(values) == 1:
        return values[0], values
    return "mixed", values


def _source_status(real_count: int, fetch_status: dict[str, dict[str, Any]], parser_failures: list[dict[str, str]]) -> str:
    statuses = [str(item.get("fetch_status") or "unknown") for item in fetch_status.values()]
    failed = [status for status in statuses if status != "reachable"]
    if parser_failures:
        return "degraded"
    if failed and real_count:
        return "degraded"
    if not failed:
        return "reachable"
    unique = sorted(set(failed))
    return unique[0] if len(unique) == 1 else "degraded"


class PronovoAgBtGuaranteesOfOriginAdapter:
    info = AdapterInfo(
        adapter_id="pronovo_ag_bt_guarantees_of_origin",
        venue=VENUE,
        market_type="renewable_fuel_guarantee_of_origin_reference",
        source="Pronovo AG Swiss BT guarantees-of-origin public registry",
        capabilities=(
            "public_market_data",
            "renewable_fuel_guarantees_of_origin",
            "renewable_gas",
            "liquid_biofuel",
            "hydrogen_import_requirements",
            "gas_certificate_import_routes",
            "source_health",
        ),
        aliases=(
            "pronovo",
            "pronovo ag",
            "swiss bt guarantees of origin",
            "swiss renewable fuel guarantees of origin",
            "shkn bt",
            "hkn bt",
            "ets ebs",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.pronovo_ag.PronovoAgBtGuaranteesOfOriginAdapter"
        ),
        quote_assets=("HKN_BT_UNIT",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        sources = (
            ("overview", str(cfg.get("source_url") or SOURCE_URL), parse_pronovo_bt_overview),
            ("gas_import", str(cfg.get("import_gas_url") or IMPORT_GAS_URL), parse_pronovo_gas_import_routes),
            (
                "hydrogen_requirements",
                str(cfg.get("hydrogen_requirements_url") or HYDROGEN_REQUIREMENTS_URL),
                parse_pronovo_hydrogen_requirements,
            ),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        for source_key, source_url, parser in sources:
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(result, source_url, source_key))
                continue
            try:
                observations.extend(
                    parser(
                        str(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                    )
                )
            except (PronovoAgParseError, TypeError, ValueError) as exc:
                message = f"Pronovo AG {source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations.append(
                    _failure_observation(result, source_url, source_key, parser_error=message)
                )

        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_state, freshness_states = _summarize_states(real_observations, "freshness_state")
        session_state, session_states = _summarize_states(real_observations, "session_status")
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1479,
                "source_status": _source_status(len(real_observations), fetch_status, parser_failures),
                "source_url": sources[0][1],
                "source_urls": [item[1] for item in sources],
                "fetch_status": fetch_status,
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_state,
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "capability_gap": "public_certificate_prices_transfers_and_order_book_not_available",
                "paper_only": True,
            },
        )


register_adapter(PronovoAgBtGuaranteesOfOriginAdapter())
