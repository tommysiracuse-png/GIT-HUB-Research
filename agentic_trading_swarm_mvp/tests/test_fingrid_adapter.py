from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.fingrid import (
    AGGREGATED_BIDS_URL,
    CAPACITY_MARKET_URL,
    DATASET_PAGE_URL,
    DOCS_URL,
    FingridMfrrAdapter,
    parse_fingrid_dataset_detail,
    parse_fingrid_market_info_dataset_ids,
)


CAPACITY_PAGE = """
<html><body>
<basic-graph id="graph-0" :settings='{"DataSetConfiguration":[{"Id":327,"Name":"Balancing Capacity (mFRR), up, hourly market, procured volume"},{"Id":328,"Name":"Balancing Capacity (mFRR), down, hourly market, procured volume"},{"Id":332,"Name":"Balancing Capacity (mFRR), up, hourly market, bids"},{"Id":331,"Name":"Balancing Capacity (mFRR), down, hourly market, bids"}],"OpenDataUrl":"https://data.fingrid.fi/en/data?datasets=327\\u0026datasets=328\\u0026datasets=332\\u0026datasets=331"}'></basic-graph>
<basic-graph id="graph-1" :settings='{"DataSetConfiguration":[{"Id":329,"Name":"Balancing Capacity Market (mFRR), up, hourly market, price"},{"Id":330,"Name":"Balancing Capacity (mFRR), down, hourly market, price"}],"OpenDataUrl":"https://data.fingrid.fi/en/data?datasets=329\\u0026datasets=330"}'></basic-graph>
<basic-graph id="graph-2" :settings='{"DataSetConfiguration":[{"Id":333,"Name":"Archive only"}],"OpenDataUrl":"https://data.fingrid.fi/en/data?datasets=333"}'></basic-graph>
</body></html>
"""

BIDS_PAGE = """
<html><body>
<basic-graph id="graph-0" :settings='{"DataSetConfiguration":[{"Id":374,"Name":"mFRR down-regulation bids"},{"Id":373,"Name":"mFRR up-regulation bids"}],"OpenDataUrl":"https://data.fingrid.fi/en/data?datasets=374\\u0026datasets=373"}'></basic-graph>
<basic-graph id="graph-1" :settings='{"DataSetConfiguration":[{"Id":243,"Name":"Legacy series"}],"OpenDataUrl":"https://data.fingrid.fi/en/"}'></basic-graph>
</body></html>
"""


def text_result(text: str, received_at: str = "2026-08-06T11:20:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 5.0,
    }


def unavailable_result(error: str = "HTTP Error 503: Service Unavailable") -> dict:
    return {
        "ok": False,
        "status": "unavailable",
        "http_status": 503,
        "error": error,
        "text": "",
        "received_at": "2026-08-06T11:20:00+00:00",
        "latency_ms": 8.0,
    }


def dataset_page_html(
    dataset_id: int,
    name: str,
    unit: str,
    data_period: str,
    latest_value: float,
    *,
    latest_start: str,
    latest_end: str,
) -> str:
    step = 3600 if data_period == "1 h" else 900
    latest_start_epoch = int(
        __import__("datetime").datetime.fromisoformat(latest_start.replace("Z", "+00:00")).timestamp()
    )
    rows = []
    for index in range(5):
        start_epoch = latest_start_epoch - (index * step)
        end_epoch = start_epoch + step
        start_time = __import__("datetime").datetime.fromtimestamp(
            start_epoch, tz=__import__("datetime").timezone.utc
        ).isoformat().replace("+00:00", "Z")
        end_time = __import__("datetime").datetime.fromtimestamp(
            end_epoch, tz=__import__("datetime").timezone.utc
        ).isoformat().replace("+00:00", "Z")
        rows.append(
            {
                "datasetId": dataset_id,
                "startTime": start_time,
                "endTime": end_time,
                "value": latest_value - index,
            }
        )
    payload = {
        "props": {
            "pageProps": {
                "datasetInfo": {
                    "statusCode": 200,
                    "data": {
                        "id": dataset_id,
                        "status": "active",
                        "organization": "Fingrid",
                        "nameEn": name,
                        "descriptionEn": f"Public sample for {name}",
                        "dataPeriodEn": data_period,
                        "updateCadenceEn": "1 d",
                        "unitEn": unit,
                        "availableFormats": ["json", "csv"],
                        "dataAvailableFromUtc": "2022-11-30T23:00:00.000Z",
                    },
                },
                "datasetDataJson": {"statusCode": 200, "data": rows, "pagination": {"total": len(rows)}},
            }
        }
    }
    return (
        "<html><body><script id=\"__NEXT_DATA__\" type=\"application/json\">"
        + json.dumps(payload, separators=(",", ":"))
        + "</script></body></html>"
    )


DATASET_PAGES = {
    327: dataset_page_html(
        327,
        "Balancing Capacity (mFRR), up, hourly market, procured volume",
        "MW",
        "1 h",
        406.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    328: dataset_page_html(
        328,
        "Balancing Capacity (mFRR), down, hourly market, procured volume",
        "MW",
        "1 h",
        700.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    329: dataset_page_html(
        329,
        "Balancing Capacity Market (mFRR), up, hourly market, price",
        "EUR/MW",
        "1 h",
        1.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    330: dataset_page_html(
        330,
        "Balancing Capacity (mFRR), down, hourly market, price",
        "EUR/MW",
        "1 h",
        1.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    331: dataset_page_html(
        331,
        "Balancing Capacity (mFRR), down, hourly market, bids",
        "MW",
        "1 h",
        2282.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    332: dataset_page_html(
        332,
        "Balancing Capacity (mFRR), up, hourly market, bids",
        "MW",
        "1 h",
        957.0,
        latest_start="2026-08-06T10:00:00Z",
        latest_end="2026-08-06T11:00:00Z",
    ),
    373: dataset_page_html(
        373,
        "mFRR up-regulation bids",
        "MW",
        "15 min",
        412.0,
        latest_start="2026-08-06T11:00:00Z",
        latest_end="2026-08-06T11:15:00Z",
    ),
    374: dataset_page_html(
        374,
        "mFRR down-regulation bids",
        "MW",
        "15 min",
        -477.0,
        latest_start="2026-08-06T11:00:00Z",
        latest_end="2026-08-06T11:15:00Z",
    ),
}


def fetch_text_for_url(url: str, timeout: int = 15, *, method: str = "GET", json_body=None) -> dict:
    del timeout, method, json_body
    if url == CAPACITY_MARKET_URL:
        return text_result(CAPACITY_PAGE)
    if url == AGGREGATED_BIDS_URL:
        return text_result(BIDS_PAGE)
    for dataset_id, page in DATASET_PAGES.items():
        if url == DATASET_PAGE_URL.format(dataset_id=dataset_id):
            return text_result(page)
    raise AssertionError(f"unexpected Fingrid fetch URL: {url}")


class FingridAdapterTests(unittest.TestCase):
    def test_parsers_normalize_market_info_and_dataset_pages(self) -> None:
        capacity_ids = parse_fingrid_market_info_dataset_ids(CAPACITY_PAGE, source_url=CAPACITY_MARKET_URL)
        self.assertEqual({327, 328, 329, 330, 331, 332}, set(capacity_ids["capacity_market"]))
        bid_ids = parse_fingrid_market_info_dataset_ids(BIDS_PAGE, source_url=AGGREGATED_BIDS_URL)
        self.assertEqual({373, 374}, set(bid_ids["aggregated_regulating_bids"]))

        row = parse_fingrid_dataset_detail(
            DATASET_PAGES[329],
            dataset_id=329,
            source_url=DATASET_PAGE_URL.format(dataset_id=329),
            market_reference_url=CAPACITY_MARKET_URL,
            received_at="2026-08-06T11:20:00+00:00",
        )
        self.assertEqual("MFRR_CAPACITY_UP_PRICE", row["symbol"])
        self.assertEqual(1.0, row["last"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("recently_published", row["session_status"])
        self.assertEqual(5, row["recent_sample_count"])
        self.assertEqual("public_fingrid_reference_only_route_needed", row["candidate_reject_reason"])

    def test_scan_preserves_real_rows_and_degraded_watch_only_evidence(self) -> None:
        adapter_id = "fingrid_mfrr_balancing_capacity"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, FingridMfrrAdapter)
        self.assertEqual(DOCS_URL, adapter.info.docs_url)
        self.assertIn("balancing_energy_bid_reference", adapter.info.capabilities)

        broken_pages = dict(DATASET_PAGES)
        broken_pages[329] = "<html>broken</html>"

        def broken_fetch(url: str, timeout: int = 15, *, method: str = "GET", json_body=None) -> dict:
            del timeout, method, json_body
            if url == CAPACITY_MARKET_URL:
                return text_result(CAPACITY_PAGE)
            if url == AGGREGATED_BIDS_URL:
                return text_result(BIDS_PAGE)
            for dataset_id, page in broken_pages.items():
                if url == DATASET_PAGE_URL.format(dataset_id=dataset_id):
                    return text_result(page)
            raise AssertionError(f"unexpected Fingrid fetch URL: {url}")

        with mock.patch("adapters.venues.fingrid.fetch_text", side_effect=fetch_text_for_url):
            batch = FingridMfrrAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(8, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["capacity_market_page"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["dataset_373"]["fetch_status"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        with mock.patch("adapters.venues.fingrid.fetch_text", side_effect=broken_fetch):
            degraded = FingridMfrrAdapter().scan({})
        self.assertEqual("degraded", degraded.metadata["source_status"])
        self.assertEqual(1, len(degraded.metadata["parser_failures"]))
        self.assertEqual("reachable", degraded.metadata["fetch_status"]["dataset_329"]["fetch_status"])
        self.assertTrue(
            any(
                row.get("symbol") == "DATASET_329_HEALTH"
                and row.get("candidate_reject_reason") == "public_fingrid_parser_failure"
                for row in degraded.observations
            )
        )

        with mock.patch(
            "adapters.venues.fingrid.fetch_text",
            side_effect=lambda url, timeout=15, method="GET", json_body=None: unavailable_result()
            if url == AGGREGATED_BIDS_URL
            else fetch_text_for_url(url, timeout, method=method, json_body=json_body),
        ):
            unavailable = FingridMfrrAdapter().scan({})
        self.assertEqual("degraded", unavailable.metadata["source_status"])
        self.assertEqual("unavailable", unavailable.metadata["fetch_status"]["aggregated_bids_page"]["fetch_status"])
        self.assertTrue(
            any(
                row.get("symbol") == "AGGREGATED_BIDS_PAGE_HEALTH"
                and row.get("fetch_status") == "unavailable"
                for row in unavailable.observations
            )
        )

    def test_runtime_discovery_and_capability_match_cover_spec_684(self) -> None:
        adapter_id = "fingrid_mfrr_balancing_capacity"
        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.fingrid.fetch_text", side_effect=fetch_text_for_url
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime, "discover_adapters", side_effect=lambda: [item for item in original_discover() if item == adapter_id]
        ):
            runtime_batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )
        report = runtime_batch.metadata["public_market_adapters"]["adapters"][0]
        self.assertEqual(adapter_id, report["adapter_id"])
        self.assertEqual("reachable", report["source_status"])
        self.assertEqual(8, report["observation_count"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #684: Fingrid",
                "market_key": "global_discovery|Fingrid",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Fingrid",
                        "public_docs_url": DOCS_URL,
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
