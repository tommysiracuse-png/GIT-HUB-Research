from __future__ import annotations

import copy
import json
import pathlib
import ssl
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
import code_evolution
import storage
from adapters.venues import common as venue_common
from adapters.registry import discover_adapters
from adapters.venues.bahrain_cross_listings import cross_listing_observations
from adapters.venues.bursa_derivatives import contract_observations
from adapters.venues.kase_futures import parse_kase_futures
from adapters.venues.nzx_dairy import parse_nzx_gdt
from adapters.venues.twse_daily import parse_twse_daily
from scan_batch import ScanBatch
from settings import DEFAULT_SETTINGS


class PublicAdapterParserTests(unittest.TestCase):
    def test_public_fetch_retries_certificate_failure_with_system_trust(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return b"[]"

        certificate_error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        system_context = mock.sentinel.system_context
        with mock.patch.object(
            venue_common.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError(certificate_error), Response()],
        ) as urlopen, mock.patch.object(
            venue_common,
            "_system_trust_context",
            return_value=system_context,
        ):
            result = venue_common.fetch_text("https://official.example.test/market-data")

        self.assertTrue(result["ok"])
        self.assertEqual("system", result["tls_trust_source"])
        self.assertEqual(2, urlopen.call_count)
        self.assertIs(system_context, urlopen.call_args_list[1].kwargs["context"])

    def test_registered_batch_is_discoverable(self) -> None:
        expected = {
            "twse_daily_public",
            "kase_futures_public_results",
            "nzx_gdt_event_reference",
            "bursa_derivatives_contract_catalog",
            "bahrain_cross_listings_catalog",
        }
        self.assertTrue(expected <= set(discover_adapters()))

    def test_twse_daily_parser_normalizes_official_row(self) -> None:
        rows = parse_twse_daily(
            [
                {
                    "Date": "1150730",
                    "Code": "2330",
                    "Name": "TSMC",
                    "OpeningPrice": "1000",
                    "HighestPrice": "1010",
                    "LowestPrice": "990",
                    "ClosingPrice": "1005",
                    "TradeVolume": "100",
                    "TradeValue": "100500",
                    "Change": "+5",
                }
            ]
        )
        self.assertEqual("TWSE:2330", rows[0]["inst_id"])
        self.assertEqual("TWD", rows[0]["quote"])
        self.assertEqual(1005.0, rows[0]["last"])

    def test_kase_futures_parser_normalizes_table(self) -> None:
        html = """
        <table><tr><th>Instrument</th><th>Settlement</th><th>Min</th><th>Max</th><th>Last</th>
        <th>Volume</th><th>Deals</th><th>Open positions</th><th>Demand</th><th>Ask</th></tr>
        <tr><td>US-9.26</td><td>520,10</td><td>519,00</td><td>521,00</td><td>520,50</td>
        <td>10,5</td><td>4</td><td>7</td><td>520,40</td><td>520,60</td></tr></table>
        """
        rows = parse_kase_futures(html)
        self.assertEqual("KASE:US-9.26", rows[0]["inst_id"])
        self.assertEqual(520.5, rows[0]["last"])
        self.assertEqual("fx_futures", rows[0]["asset_class"])

    def test_nzx_gdt_parser_normalizes_event_prices(self) -> None:
        html = """
        <table><tr><th>Products</th><th>Event 401</th><th>Event 400</th><th>Change</th></tr>
        <tr><td>Whole Milk Powder</td><td>3,900</td><td>3,800</td><td>+2.6%</td></tr></table>
        """
        rows = parse_nzx_gdt(html)
        self.assertEqual("NZX_GDT:WHOLE_MILK_POWDER", rows[0]["inst_id"])
        self.assertEqual(3900.0, rows[0]["last"])
        self.assertEqual("Event 401", rows[0]["event_id"])

    def test_catalog_adapters_never_invent_prices(self) -> None:
        rows = contract_observations("blocked") + cross_listing_observations("reachable")
        self.assertTrue(rows)
        self.assertTrue(all(row["last"] == 0.0 for row in rows))
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["candidate_reject_reason"] == "public_quote_endpoint_not_available" for row in rows))

    def test_runtime_combines_registered_batches(self) -> None:
        class FakeAdapter:
            class info:
                adapter_id = "fake_public"
                venue = "FAKE"
                market_type = "equity"
                source = "fixture"
                active = True
                default_cache_minutes = 0
                runtime_entrypoint = "fake.scan"
                docs_url = "https://example.test"

            def scan(self, _settings):
                return ScanBatch(
                    source="fixture",
                    candidates=[],
                    observations=[{"venue": "FAKE", "inst_id": "FAKE:X", "last": 1.0}],
                    metadata={"source_status": "reachable"},
                )

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["public_market_adapters"] = {"enabled": True, "workers": 1}
        fake = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache")
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", return_value=["fake_public"]), mock.patch.object(
            adapter_runtime, "get_adapter", return_value=fake
        ):
            batch = adapter_runtime.build_scan_batch(settings)
        self.assertEqual(1, len(batch.observations))
        self.assertEqual("FAKE:X", batch.observations[0]["inst_id"])


class AdapterCapabilityTests(unittest.TestCase):
    def test_existing_adapter_is_resolved_but_missing_depth_remains_gap(self) -> None:
        daily = {
            "title": "TWSE public daily price adapter",
            "market_key": "global_discovery|Taiwan Stock Exchange",
            "spec": {
                "candidate": {
                    "venue_or_source": "Taiwan Stock Exchange",
                    "public_docs_url": "https://openapi.twse.com.tw/",
                    "why_interesting": "daily price coverage",
                }
            },
        }
        depth = copy.deepcopy(daily)
        depth["title"] = "TWSE order book depth adapter"
        depth["spec"]["candidate"]["why_interesting"] = "order book depth"
        self.assertEqual("fully_covered", adapter_capabilities.match_adapter_spec(daily)["match_status"])
        gap = adapter_capabilities.match_adapter_spec(depth)
        self.assertEqual("partial_capability_gap", gap["match_status"])
        self.assertIn("order_book", gap["missing_capabilities"])

        realtime = copy.deepcopy(daily)
        realtime["title"] = "TWSE real-time five-second market snapshot"
        realtime["spec"]["candidate"]["why_interesting"] = "intraday entry-quality price coverage"
        gap = adapter_capabilities.match_adapter_spec(realtime)
        self.assertEqual("partial_capability_gap", gap["match_status"])
        self.assertIn("entry_quality_quote", gap["missing_capabilities"])

    def test_reconciliation_closes_covered_spec_and_supersedes_duplicate(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        candidate = {
            "venue_or_source": "Taiwan Stock Exchange",
            "public_docs_url": "https://openapi.twse.com.tw/",
            "why_interesting": "daily prices",
        }
        for idx in range(2):
            storage.add_adapter_spec(
                conn,
                f"rec-{idx}",
                "global_discovery|Taiwan Stock Exchange",
                90 - idx,
                f"TWSE public daily price adapter {idx}",
                {"candidate": candidate},
                {},
            )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_capabilities, "REPORT_JSON", pathlib.Path(tmp) / "inventory.json"
        ), mock.patch.object(adapter_capabilities, "REPORT_MD", pathlib.Path(tmp) / "inventory.md"):
            report = adapter_capabilities.reconcile_adapter_specs(conn)
        statuses = [row["status"] for row in conn.execute("select status from adapter_specs order by id")]
        self.assertEqual("resolved_existing_adapter_capability", statuses[0])
        self.assertEqual("superseded_duplicate_adapter_spec", statuses[1])
        self.assertEqual(2, report["summary"]["specs_reconciled"])
        conn.close()

    def test_reconciliation_reopens_resolved_spec_when_capability_is_not_deployed(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        storage.add_adapter_spec(
            conn,
            "rec-realtime",
            "global_discovery|Taiwan Stock Exchange",
            92,
            "TWSE real-time five-second market snapshot",
            {
                "candidate": {
                    "venue_or_source": "Taiwan Stock Exchange",
                    "public_docs_url": "https://openapi.twse.com.tw/",
                    "why_interesting": "intraday entry-quality price coverage",
                }
            },
            {},
        )
        conn.execute("update adapter_specs set status = 'resolved_existing_adapter_capability'")
        conn.commit()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_capabilities, "REPORT_JSON", pathlib.Path(tmp) / "inventory.json"
        ), mock.patch.object(adapter_capabilities, "REPORT_MD", pathlib.Path(tmp) / "inventory.md"):
            adapter_capabilities.reconcile_adapter_specs(conn)
        row = conn.execute("select status, evidence_json from adapter_specs").fetchone()
        self.assertEqual("adapter_capability_gap", row["status"])
        evidence = json.loads(row["evidence_json"])
        self.assertIn(
            "entry_quality_quote",
            evidence["adapter_capability_reconciliation"]["missing_capabilities"],
        )
        conn.close()

    def test_auto_coder_accepts_new_adapter_plugin_as_runtime_integration(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Add a public example exchange adapter",
            "rationale": "A sourced global discovery has public no-key market data.",
            "evidence": {"source_url": "https://example.test/docs"},
            "change_category": "public_data_adapter",
            "implementation_mode": "runtime_active",
            "expected_files": [
                "src/adapters/venues/example_exchange.py",
                "tests/test_public_market_adapters.py",
            ],
            "tests_to_run": ["python -m unittest tests.test_public_market_adapters"],
            "proposed_change": "Add and register a normalized ScanBatch adapter with parser tests.",
        }
        preflight = code_evolution.preflight_proposal(payload, DEFAULT_SETTINGS, root=ROOT)
        self.assertIn("src/adapters/venues/example_exchange.py", preflight["target_files"])
        self.assertEqual("integrated", preflight["quality_scorecard"]["runtime_integration_status"])
        self.assertFalse(preflight["quality_scorecard"]["reject_before_model_call"])


if __name__ == "__main__":
    unittest.main()
