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
from adapters.venues.bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi import (
    APPROVAL_NUMBER,
    MARKET_URL,
    ORGANIZER_URL,
    BappebtiApkarkusiAdapter,
    parse_bappebti_apkarkusi_auction_prices,
    parse_bappebti_apkarkusi_organizer_listing,
)


ORGANIZER_PAGE = """
<html><body><table>
  <tr><th>Nama Penyelenggara</th><th>Alamat</th><th>Persetujuan Bappebti</th></tr>
  <tr>
    <td>Koperasi Assosiasi Petani Karet Kuantan Singingi</td>
    <td>Jl. Perintis Kemerdekaan Km 2, Kuantan Singingi - Riau</td>
    <td>01/Bappebti/Kep-PL/SP/07/2020</td>
  </tr>
</table></body></html>
"""

MARKET_PAGE = """
<html><body><table>
  <tr><th>Tanggal Lelang</th><th>Komoditas</th><th>Harga (Rp/Kg)</th><th>Peserta</th><th>Volume (Kg)</th></tr>
  <tr><td>05/08/2026</td><td>Karet Bokar</td><td>12.345</td><td>18</td><td>4.500</td></tr>
</table></body></html>
"""


def fetch_result(text: str, received_at: str = "2026-08-05T12:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class BappebtiApkarkusiAdapterTests(unittest.TestCase):
    def test_plugin_is_discoverable_and_paper_only(self) -> None:
        adapter_id = "bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi"

        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)

        self.assertIsInstance(adapter, BappebtiApkarkusiAdapter)
        self.assertEqual(ORGANIZER_URL, adapter.info.docs_url)
        self.assertIn("published_auction_price_when_available", adapter.info.capabilities)
        self.assertNotIn("order_entry", adapter.info.capabilities)

    def test_parsers_normalize_registration_and_published_bokar_price(self) -> None:
        registry_rows = parse_bappebti_apkarkusi_organizer_listing(
            ORGANIZER_PAGE, received_at="2026-08-05T12:00:00+00:00"
        )
        price_rows = parse_bappebti_apkarkusi_auction_prices(
            MARKET_PAGE, received_at="2026-08-05T12:00:00+00:00"
        )

        self.assertEqual(1, len(registry_rows))
        self.assertEqual(APPROVAL_NUMBER, registry_rows[0]["bappebti_approval_number"])
        self.assertFalse(registry_rows[0]["price_available"])
        self.assertEqual("market_session_unknown", registry_rows[0]["session_status"])
        self.assertEqual(ORGANIZER_URL, registry_rows[0]["source_url"])

        self.assertEqual(1, len(price_rows))
        price = price_rows[0]
        self.assertEqual("BAPPEBTI_APKARKUSI:BOKAR:2026-08-05:1", price["inst_id"])
        self.assertEqual(12345.0, price["last"])
        self.assertEqual(18, price["participant_count"])
        self.assertEqual(4500.0, price["lot_volume_kg"])
        self.assertEqual("fresh", price["freshness_state"])
        self.assertEqual("completed", price["session_status"])
        self.assertEqual(MARKET_URL, price["source_url"])
        self.assertEqual("watch_only", price["direction"])
        self.assertEqual("synthetic_research_only", price["paper_route_status"])

    def test_adapter_preserves_fetch_and_parser_failure_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-05T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi.fetch_text",
            side_effect=[fetch_result("<html>replacement page</html>"), blocked],
        ):
            batch = BappebtiApkarkusiAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["organizer_registry"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["market_page"]["fetch_status"])
        self.assertIn("organiser marker", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual("public_bappebti_apkarkusi_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual("public_bappebti_apkarkusi_source_unavailable", batch.observations[1]["candidate_reject_reason"])

    def test_adapter_runtime_auto_discovers_normalized_batch(self) -> None:
        adapter_id = "bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_apkarkusi() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.bappebti_koperasi_assosiasi_petani_karet_kuantan_singingi_apkarkusi.fetch_text",
            side_effect=[fetch_result(ORGANIZER_PAGE), fetch_result(MARKET_PAGE)],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_apkarkusi):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual(2, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(1378, report["adapters"][0]["adapter_spec_id"])


if __name__ == "__main__":
    unittest.main()
