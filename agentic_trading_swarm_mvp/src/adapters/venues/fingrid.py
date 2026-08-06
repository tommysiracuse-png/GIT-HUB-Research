"""Fingrid public mFRR capacity and balancing-bid references.

Fingrid publishes public market-information pages for the Finnish mFRR
balancing capacity market and aggregated balancing-energy bids. Those public
pages expose the current dataset identifiers, while the unauthenticated dataset
detail pages include a recent sample of observations in their Next.js payload.
This adapter normalizes those public references into watch-only, paper-only
observations without using Fingrid's authenticated REST API.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, slug, utc_now
from scan_batch import ScanBatch


DOCS_URL = "https://www.fingrid.fi/en/electricity-market/reserves/reserve-products/balancing-energy-and-balancing-capacity-markets-mfrr/"
CAPACITY_MARKET_URL = "https://www.fingrid.fi/en/electricity-market-information/reserve-market-information/balancing-capacity-market-procurement/"
AGGREGATED_BIDS_URL = "https://www.fingrid.fi/en/electricity-market-information/reserve-market-information/aggregated-regulating-bids/"
DEVELOPER_INFO_URL = "https://developers.fingrid.fi/information_exchange"
DATASET_PAGE_URL = "https://data.fingrid.fi/en/datasets/{dataset_id}"
VENUE = "FINGRID"
MARKET_SURFACE = "fingrid_mfrr_balancing_capacity_and_bid_market"
HELSINKI = dt.timezone(dt.timedelta(hours=2), name="Europe/Helsinki")

EXPECTED_CAPACITY_DATASETS = {327, 328, 329, 330, 331, 332}
EXPECTED_BID_DATASETS = {373, 374}

DATASET_KIND = {
    327: {
        "symbol": "MFRR_CAPACITY_UP_PROCURED_VOLUME",
        "base": "MFRR_CAPACITY_UP",
        "quote": "MW",
        "market_type": "balancing_capacity_procured_volume_reference",
        "trade_type": "official_balancing_capacity_procurement_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "up",
        "value_role": "procured_volume_mw",
        "reference_group": "capacity_market",
    },
    328: {
        "symbol": "MFRR_CAPACITY_DOWN_PROCURED_VOLUME",
        "base": "MFRR_CAPACITY_DOWN",
        "quote": "MW",
        "market_type": "balancing_capacity_procured_volume_reference",
        "trade_type": "official_balancing_capacity_procurement_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "down",
        "value_role": "procured_volume_mw",
        "reference_group": "capacity_market",
    },
    329: {
        "symbol": "MFRR_CAPACITY_UP_PRICE",
        "base": "MFRR_CAPACITY_UP",
        "quote": "EUR_PER_MW",
        "market_type": "balancing_capacity_price_reference",
        "trade_type": "official_balancing_capacity_price_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "up",
        "value_role": "capacity_price_eur_per_mw",
        "reference_group": "capacity_market",
    },
    330: {
        "symbol": "MFRR_CAPACITY_DOWN_PRICE",
        "base": "MFRR_CAPACITY_DOWN",
        "quote": "EUR_PER_MW",
        "market_type": "balancing_capacity_price_reference",
        "trade_type": "official_balancing_capacity_price_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "down",
        "value_role": "capacity_price_eur_per_mw",
        "reference_group": "capacity_market",
    },
    331: {
        "symbol": "MFRR_CAPACITY_DOWN_BIDS",
        "base": "MFRR_CAPACITY_DOWN",
        "quote": "MW",
        "market_type": "balancing_capacity_bid_reference",
        "trade_type": "official_balancing_capacity_bid_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "down",
        "value_role": "capacity_bids_mw",
        "reference_group": "capacity_market",
    },
    332: {
        "symbol": "MFRR_CAPACITY_UP_BIDS",
        "base": "MFRR_CAPACITY_UP",
        "quote": "MW",
        "market_type": "balancing_capacity_bid_reference",
        "trade_type": "official_balancing_capacity_bid_reference",
        "asset_class": "electricity_reserve_capacity",
        "direction_label": "up",
        "value_role": "capacity_bids_mw",
        "reference_group": "capacity_market",
    },
    373: {
        "symbol": "MFRR_UP_REGULATION_BIDS",
        "base": "MFRR_ENERGY_UP",
        "quote": "MW",
        "market_type": "balancing_energy_bid_reference",
        "trade_type": "official_aggregated_balancing_energy_bid_reference",
        "asset_class": "electricity_balancing_energy",
        "direction_label": "up",
        "value_role": "aggregated_bid_volume_mw",
        "reference_group": "aggregated_regulating_bids",
    },
    374: {
        "symbol": "MFRR_DOWN_REGULATION_BIDS",
        "base": "MFRR_ENERGY_DOWN",
        "quote": "MW",
        "market_type": "balancing_energy_bid_reference",
        "trade_type": "official_aggregated_balancing_energy_bid_reference",
        "asset_class": "electricity_balancing_energy",
        "direction_label": "down",
        "value_role": "aggregated_bid_volume_mw",
        "reference_group": "aggregated_regulating_bids",
    },
}


class FingridParseError(ValueError):
    """Raised when public Fingrid pages no longer expose expected fields."""


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FingridParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _time(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise FingridParseError(f"invalid {field}: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _period_seconds(value: str | None) -> int:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+)\s*(min|h|d)", text)
    if not match:
        return 3600
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "min":
        return amount * 60
    if unit == "h":
        return amount * 3600
    return amount * 86400


def _freshness(end_at: dt.datetime, fetched_at: dt.datetime, period_seconds: int) -> tuple[str, float]:
    age_seconds = max(0.0, (fetched_at - end_at).total_seconds())
    stale_after = max(float(period_seconds) * 8.0, 7200.0)
    return ("fresh" if age_seconds <= stale_after else "stale", round(age_seconds, 3))


def _session_status(start_at: dt.datetime, end_at: dt.datetime, fetched_at: dt.datetime, period_seconds: int) -> str:
    if fetched_at < start_at:
        return "scheduled"
    if start_at <= fetched_at < end_at:
        return "active_market_time_unit"
    if (fetched_at - end_at).total_seconds() <= max(float(period_seconds) * 2.0, 1800.0):
        return "recently_published"
    return "historical_reference"


def parse_fingrid_market_info_dataset_ids(
    document: str,
    *,
    source_url: str,
) -> dict[str, dict[int, str]]:
    """Extract dataset ids from Fingrid's public market-information graphs."""

    if not isinstance(document, str) or not document.strip():
        raise FingridParseError("market information response is empty")
    matches = re.findall(r"<basic-graph\b[^>]*:settings='([^']+)'", document)
    if not matches:
        raise FingridParseError("market information page did not expose any basic-graph settings")
    groups = {"capacity_market": {}, "aggregated_regulating_bids": {}}
    for payload in matches:
        try:
            settings = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FingridParseError(f"graph settings JSON was invalid: {exc}") from exc
        for item in settings.get("DataSetConfiguration") or []:
            try:
                dataset_id = int(item.get("Id"))
            except (TypeError, ValueError):
                continue
            name = str(item.get("Name") or "")
            if dataset_id in EXPECTED_CAPACITY_DATASETS:
                groups["capacity_market"][dataset_id] = name
            elif dataset_id in EXPECTED_BID_DATASETS:
                groups["aggregated_regulating_bids"][dataset_id] = name
    if source_url == CAPACITY_MARKET_URL and set(groups["capacity_market"]) != EXPECTED_CAPACITY_DATASETS:
        missing = sorted(EXPECTED_CAPACITY_DATASETS - set(groups["capacity_market"]))
        raise FingridParseError(f"capacity market page was missing dataset ids: {missing}")
    if source_url == AGGREGATED_BIDS_URL and set(groups["aggregated_regulating_bids"]) != EXPECTED_BID_DATASETS:
        missing = sorted(EXPECTED_BID_DATASETS - set(groups["aggregated_regulating_bids"]))
        raise FingridParseError(f"aggregated bids page was missing dataset ids: {missing}")
    return groups


def _next_data(document: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', document, re.S)
    if not match:
        raise FingridParseError("dataset page did not contain __NEXT_DATA__ JSON")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise FingridParseError(f"dataset page JSON was invalid: {exc}") from exc
    try:
        page_props = payload["props"]["pageProps"]
    except (KeyError, TypeError) as exc:
        raise FingridParseError("dataset page props were not available") from exc
    if not isinstance(page_props, dict):
        raise FingridParseError("dataset page props were not a JSON object")
    return page_props


def parse_fingrid_dataset_detail(
    document: str,
    *,
    dataset_id: int,
    source_url: str,
    market_reference_url: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Normalize the latest public sample row from a Fingrid dataset page."""

    if dataset_id not in DATASET_KIND:
        raise FingridParseError(f"unsupported Fingrid dataset id: {dataset_id}")
    if not isinstance(document, str) or not document.strip():
        raise FingridParseError("dataset detail response is empty")
    page_props = _next_data(document)
    info_wrapper = page_props.get("datasetInfo")
    data_wrapper = page_props.get("datasetDataJson")
    if not isinstance(info_wrapper, dict) or not isinstance(info_wrapper.get("data"), dict):
        raise FingridParseError("dataset page did not contain datasetInfo.data")
    if not isinstance(data_wrapper, dict) or not isinstance(data_wrapper.get("data"), list):
        raise FingridParseError("dataset page did not contain datasetDataJson.data")
    info = dict(info_wrapper["data"])
    if int(info.get("id") or -1) != dataset_id:
        raise FingridParseError(
            f"dataset page id mismatch: expected {dataset_id}, got {info.get('id')!r}"
        )
    rows = list(data_wrapper.get("data") or [])
    if not rows:
        raise FingridParseError(f"dataset {dataset_id} did not expose any sample observations")
    ordered_rows = sorted(
        (row for row in rows if isinstance(row, dict)),
        key=lambda row: str(row.get("startTime") or ""),
        reverse=True,
    )
    if not ordered_rows:
        raise FingridParseError(f"dataset {dataset_id} sample observations were malformed")
    latest = ordered_rows[0]
    if int(latest.get("datasetId") or -1) != dataset_id:
        raise FingridParseError(
            f"sample row datasetId mismatch: expected {dataset_id}, got {latest.get('datasetId')!r}"
        )
    value = latest.get("value")
    if not isinstance(value, (int, float)):
        raise FingridParseError(f"dataset {dataset_id} latest value was not numeric")
    fetched_at = _received_time(received_at)
    start_at = _time(str(latest.get("startTime") or ""), "startTime")
    end_at = _time(str(latest.get("endTime") or ""), "endTime")
    period_seconds = _period_seconds(info.get("dataPeriodEn"))
    freshness_state, freshness_age_seconds = _freshness(end_at, fetched_at, period_seconds)
    session_status = _session_status(start_at, end_at, fetched_at, period_seconds)
    spec = DATASET_KIND[dataset_id]
    inst_id = f"{VENUE}:{slug(spec['symbol'])}:{dataset_id}"
    recent_values = [float(row.get("value")) for row in ordered_rows[:5] if isinstance(row.get("value"), (int, float))]
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": spec["symbol"],
        "name": str(info.get("nameEn") or spec["symbol"]),
        "base": spec["base"],
        "quote": spec["quote"],
        "market_type": spec["market_type"],
        "market_surface": MARKET_SURFACE,
        "asset_class": spec["asset_class"],
        "trade_type": spec["trade_type"],
        "direction": "watch_only",
        "last": float(value),
        "unit": str(info.get("unitEn") or ""),
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_mfrr_market_reference",
        "freshness_state": freshness_state,
        "freshness_basis": "fingrid_dataset_sample_interval_end",
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "start_time": start_at.isoformat(),
        "end_time": end_at.isoformat(),
        "source_url": source_url,
        "market_reference_url": market_reference_url,
        "docs_url": DOCS_URL,
        "source_dataset_id": dataset_id,
        "source_dataset_name": str(info.get("nameEn") or ""),
        "dataset_status": str(info.get("status") or ""),
        "update_cadence": str(info.get("updateCadenceEn") or ""),
        "time_period": str(info.get("dataPeriodEn") or ""),
        "data_available_from": info.get("dataAvailableFromUtc"),
        "direction_label": spec["direction_label"],
        "reference_group": spec["reference_group"],
        "value_role": spec["value_role"],
        "recent_sample_count": len(ordered_rows[:5]),
        "recent_values": recent_values,
        "sample_latest_local_date": end_at.astimezone(HELSINKI).date().isoformat(),
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": False,
        "candidate_reject_reason": "public_fingrid_reference_only_route_needed",
    }


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
    source_key: str,
    source_url: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{slug(source_key)}:HEALTH",
            "instrument_id": f"{VENUE}:{slug(source_key)}:HEALTH",
            "symbol": f"{slug(source_key)}_HEALTH",
            "base": "MFRR_SOURCE_HEALTH",
            "quote": "N/A",
            "market_type": "market_data_health",
            "trade_type": "official_market_reference",
            "source_key": source_key,
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_fingrid_parser_failure" if parser_error else "public_fingrid_source_unavailable"
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
    return (values[0], values) if len(values) == 1 else ("mixed", values)


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


class FingridMfrrAdapter:
    info = AdapterInfo(
        adapter_id="fingrid_mfrr_balancing_capacity",
        venue=VENUE,
        market_type="balancing_capacity_market_reference",
        source="Fingrid public mFRR balancing capacity market and aggregated balancing bids",
        capabilities=(
            "public_market_data",
            "balancing_capacity_market",
            "balancing_energy_bid_reference",
            "auction_results",
            "source_health",
        ),
        aliases=(
            "fingrid",
            "fingrid mfrr",
            "mfrr balancing capacity market",
            "aggregated regulating bids",
            "balancing energy bids",
        ),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.fingrid.FingridMfrrAdapter",
        quote_assets=("MW", "EUR_PER_MW"),
        default_cache_minutes=30,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        dataset_sources: dict[int, str] = {}
        dataset_reference_pages: dict[int, str] = {}

        page_sources = (
            ("capacity_market_page", str(cfg.get("capacity_market_url") or CAPACITY_MARKET_URL), "capacity_market"),
            ("aggregated_bids_page", str(cfg.get("aggregated_bids_url") or AGGREGATED_BIDS_URL), "aggregated_regulating_bids"),
        )
        for source_key, source_url, group in page_sources:
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(result, source_key=source_key, source_url=source_url))
                continue
            try:
                discovered = parse_fingrid_market_info_dataset_ids(str(result.get("text") or ""), source_url=source_url)
            except (FingridParseError, TypeError, ValueError) as exc:
                message = f"Fingrid {source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations.append(
                    _failure_observation(result, source_key=source_key, source_url=source_url, parser_error=message)
                )
                continue
            for dataset_id in sorted(discovered.get(group) or {}):
                dataset_sources[dataset_id] = DATASET_PAGE_URL.format(dataset_id=dataset_id)
                dataset_reference_pages[dataset_id] = source_url

        dataset_ids = sorted(dataset_sources)
        if not dataset_ids and not parser_failures:
            parser_failures.append(
                {
                    "source_url": CAPACITY_MARKET_URL,
                    "error": "Fingrid dataset discovery produced no dataset ids",
                }
            )

        for dataset_id in dataset_ids:
            source_url = dataset_sources[dataset_id]
            result = fetch_text(source_url, timeout)
            source_key = f"dataset_{dataset_id}"
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(result, source_key=source_key, source_url=source_url))
                continue
            try:
                observations.append(
                    parse_fingrid_dataset_detail(
                        str(result.get("text") or ""),
                        dataset_id=dataset_id,
                        source_url=source_url,
                        market_reference_url=dataset_reference_pages[dataset_id],
                        received_at=result.get("received_at"),
                    )
                )
            except (FingridParseError, TypeError, ValueError) as exc:
                message = f"Fingrid dataset {dataset_id} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations.append(
                    _failure_observation(result, source_key=source_key, source_url=source_url, parser_error=message)
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
                "adapter_spec_id": 684,
                "source_status": _source_status(len(real_observations), fetch_status, parser_failures),
                "fetch_status": fetch_status,
                "source_url": DOCS_URL,
                "source_urls": [
                    DOCS_URL,
                    CAPACITY_MARKET_URL,
                    AGGREGATED_BIDS_URL,
                    DEVELOPER_INFO_URL,
                    *[dataset_sources[item] for item in dataset_ids],
                ],
                "market_reference_urls": [CAPACITY_MARKET_URL, AGGREGATED_BIDS_URL],
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_state,
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "dataset_ids": dataset_ids,
                "capability_gap": "public_no_key_reference_only_no_anonymous_order_entry",
                "paper_only": True,
            },
        )


register_adapter(FingridMfrrAdapter())
