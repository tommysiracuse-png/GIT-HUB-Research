from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.nsw_department_of_climate_change_energy_the_environment_and_water import (
    PUBLIC_REGISTERS_URL,
    SUPPLY_EXPORT_URL,
    TRANSACTIONS_EXPORT_URL,
    NswDepartmentOfClimateChangeEnergyTheEnvironmentAndWaterAdapter,
    parse_nsw_biodiversity_credit_supply,
    parse_nsw_biodiversity_credit_transactions,
)


def _supply_fixture() -> bytes:
    return b'''<?xml version="1.0"?><Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
    <Worksheet><Table><Row>
    <Cell><Data>Credit ID</Data></Cell><Cell><Data>Credit status</Data></Cell>
    <Cell><Data>Number of credits</Data></Cell><Cell><Data>Date Credits Issued</Data></Cell>
    <Cell><Data>IBRA Subregion</Data></Cell><Cell><Data>Ecosystem or Species</Data></Cell>
    <Cell><Data>Species Scientific Name</Data></Cell><Cell><Data>Offset Trading Group</Data></Cell>
    </Row><Row><Cell><Data>CR-10001</Data></Cell><Cell><Data>Pending</Data></Cell>
    <Cell><Data>125.5</Data></Cell><Cell><Data>2026-06-10</Data></Cell><Cell><Data>Hunter</Data></Cell>
    <Cell><Data>Species</Data></Cell><Cell><Data>Example species</Data></Cell><Cell><Data>OTG 1</Data></Cell>
    </Row><Row><Cell><Data>CR-10002</Data></Cell><Cell><Data>Equivalent BioBanking</Data></Cell>
    <Cell><Data>12</Data></Cell><Cell><Data>2026-06-11</Data></Cell><Cell><Data>Monaro</Data></Cell>
    <Cell><Data>Ecosystem</Data></Cell></Row></Table></Worksheet></Workbook>'''


def _transactions_fixture() -> bytes:
    return b'''<html><body><table><tr>
    <th>Transaction Date</th><th>Transaction ID</th><th>Transaction Status</th>
    <th>Transaction Type</th><th>Number Of Credits</th><th>Price Per Credit (Ex-Gst)</th>
    <th>Plant Community Type</th><th>Sub Region</th><th>Scientific Name</th></tr>
    <tr><td>July 3, 2025</td><td>="00049452"</td><td>Completed</td><td>Transfer</td>
    <td>261</td><td>$1,250.50</td><td>Woodland</td><td>Hunter</td><td>Example species</td></tr>
    </table></body></html>'''


class NswBiodiversityOffsetsAdapterTests(unittest.TestCase):
    def test_parsers_normalize_supply_status_and_transaction_sale_price(self) -> None:
        supply = parse_nsw_biodiversity_credit_supply(
            _supply_fixture().replace(b"Example species", b"Example & species"),
            received_at="2026-08-05T12:00:00+00:00",
        )
        self.assertEqual(2, len(supply))
        self.assertEqual("Pending", supply[0]["credit_status"])
        self.assertEqual(125.5, supply[0]["credit_quantity"])
        self.assertEqual("Equivalent BioBanking", supply[1]["credit_status"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in supply))
        self.assertNotIn("contact_email", supply[0])

        transactions = parse_nsw_biodiversity_credit_transactions(
            _transactions_fixture(), received_at="2026-08-05T12:00:00+00:00"
        )
        self.assertEqual(1, len(transactions))
        row = transactions[0]
        self.assertEqual("00049452", row["transaction_id"])
        self.assertEqual(1250.5, row["sale_price_per_credit_aud_ex_gst"])
        self.assertEqual(326380.5, row["transaction_value_aud_ex_gst"])
        self.assertEqual("stale", row["freshness_state"])
        self.assertEqual("watch_only", row["direction"])

    def test_plugin_discovery_runtime_and_failure_evidence(self) -> None:
        adapter_id = "nsw_department_of_climate_change_energy_the_environment_and_water"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, NswDepartmentOfClimateChangeEnergyTheEnvironmentAndWaterAdapter)
        self.assertEqual(PUBLIC_REGISTERS_URL, adapter.info.docs_url)
        self.assertIn("credit_transaction_sale_price", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

        def fetch(url: str, _timeout: int, *, max_bytes: int) -> dict:
            body = _supply_fixture() if url == SUPPLY_EXPORT_URL else _transactions_fixture()
            return {"ok": True, "status": "reachable", "http_status": 200, "content": body,
                    "received_at": "2026-08-05T12:00:00+00:00", "latency_ms": 1.0,
                    "content_type": "application/vnd.ms-excel"}

        with mock.patch(
            "adapters.venues.nsw_department_of_climate_change_energy_the_environment_and_water.fetch_bytes",
            side_effect=fetch,
        ):
            batch = adapter.scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(1539, batch.metadata["adapter_spec_id"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["credit_supply"]["fetch_status"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        malformed = {"ok": True, "status": "reachable", "http_status": 200, "content": b"not a table",
                     "received_at": "2026-08-05T12:00:00+00:00", "latency_ms": 1.0}
        with mock.patch(
            "adapters.venues.nsw_department_of_climate_change_energy_the_environment_and_water.fetch_bytes",
            return_value=malformed,
        ):
            failed = adapter.scan({})
        self.assertEqual("degraded", failed.metadata["source_status"])
        self.assertEqual(2, len(failed.metadata["parser_failures"]))
        self.assertTrue(all(row["direction"] == "watch_only" for row in failed.observations))

        original_discover = adapter_runtime.discover_adapters

        def discover_only_nsw() -> list[str]:
            return [item for item in original_discover() if item == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.nsw_department_of_climate_change_energy_the_environment_and_water.fetch_bytes",
            side_effect=fetch,
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_nsw):
            runtime_batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1,
                 "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )
        self.assertEqual(3, len(runtime_batch.observations))
        self.assertTrue(all(row["venue"] == "NSW_DCCEEW" for row in runtime_batch.observations))


if __name__ == "__main__":
    unittest.main()
