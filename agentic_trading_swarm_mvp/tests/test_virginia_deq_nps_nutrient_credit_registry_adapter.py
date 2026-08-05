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
from adapters.venues.virginia_deq_nps_nutrient_credit_registry import (
    BANK_QUERY_URL,
    NUTRIENT_TRADING_URL,
    REGULATION_URL,
    VirginiaDeqNpsNutrientCreditRegistryAdapter,
    parse_virginia_nps_nutrient_banks,
)


PAYLOAD = {
    "fields": [
        {"name": "WQT_PROJECT_ID"},
        {"name": "WQT_PROJECT_NAME"},
        {"name": "PROJECT_STATUS_NAME"},
        {"name": "TOTAL_AVAIL_PHOS"},
    ],
    "features": [
        {
            "attributes": {
                "WQT_PROJECT_ID": 516,
                "WQT_PROJECT_NAME": "Sterling (020802070301)",
                "PROJECT_STATUS_NAME": "Pending",
                "PROJECT_CAT_NAME": "NPS",
                "STATE_ABBREV_LIST": "VA",
                "NUM_SA": 2,
                "LONGITUDE": -78.496744,
                "LATITUDE": 37.184467,
                "TOTAL_AVAIL_PHOS": 8.94,
                "TOTAL_PEND_OF_PHOS": 0,
                "TOTAL_POTENTIAL_OF_PHOS": 18.24,
            }
        },
        {
            "attributes": {
                "WQT_PROJECT_ID": 492,
                "WQT_PROJECT_NAME": "Upper Brandon Lodge",
                "PROJECT_STATUS_NAME": "Approved",
                "PROJECT_CAT_NAME": "NPS",
                "STATE_ABBREV_LIST": "VA",
                "NUM_SA": 2,
                "LONGITUDE": -77.03344,
                "LATITUDE": 37.288299,
                "TOTAL_AVAIL_PHOS": None,
                "TOTAL_PEND_OF_PHOS": 0,
                "TOTAL_POTENTIAL_OF_PHOS": None,
            }
        },
    ],
}


def fetch_result(payload: dict = PAYLOAD) -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": json.dumps(payload),
        "received_at": "2026-08-05T12:00:00+00:00",
        "latency_ms": 4.0,
    }


class VirginiaDeqNpsNutrientCreditRegistryAdapterTests(unittest.TestCase):
    def test_parser_preserves_project_status_geography_and_credit_availability(self) -> None:
        rows = parse_virginia_nps_nutrient_banks(
            PAYLOAD, received_at="2026-08-05T12:00:00+00:00"
        )
        pending, unreported = rows

        self.assertEqual("VA_DEQ_NPS_NUTRIENT:NPS_BANK:516", pending["inst_id"])
        self.assertEqual("Pending", pending["project_status"])
        self.assertEqual(8.94, pending["available_phosphorus_credits"])
        self.assertEqual(0.0, pending["pending_phosphorus_credits"])
        self.assertEqual(18.24, pending["potential_phosphorus_credits"])
        self.assertEqual("020802070301", pending["huc"])
        self.assertEqual(-78.496744, pending["geography"]["longitude"])
        self.assertTrue(pending["pending_release_watch"])
        self.assertEqual(BANK_QUERY_URL, pending["source_url"])
        self.assertEqual(REGULATION_URL, pending["source_regulation_url"])
        self.assertEqual("fresh", pending["freshness_state"])
        self.assertEqual("public_registry_snapshot", pending["session_status"])
        self.assertEqual("watch_only", pending["direction"])

        self.assertIsNone(unreported["available_phosphorus_credits"])
        self.assertFalse(unreported["available_phosphorus_credits_reported"])
        self.assertIsNone(unreported["last"])

    def test_scan_preserves_parser_failure_and_unavailable_source_as_watch_only_evidence(self) -> None:
        malformed = fetch_result({"features": []})
        with mock.patch(
            "adapters.venues.virginia_deq_nps_nutrient_credit_registry.fetch_text",
            return_value=malformed,
        ):
            parser_batch = VirginiaDeqNpsNutrientCreditRegistryAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["nps_banks"]["fetch_status"])
        self.assertIn("parser failed", parser_batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual("unknown", parser_batch.observations[0]["freshness_state"])
        self.assertIsNotNone(parser_batch.observations[0]["parser_failure"])

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-05T12:00:01+00:00",
            "latency_ms": 6.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.virginia_deq_nps_nutrient_credit_registry.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = VirginiaDeqNpsNutrientCreditRegistryAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual("blocked", unavailable_batch.metadata["fetch_status"]["nps_banks"]["fetch_status"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual("unknown", unavailable_batch.observations[0]["session_status"])

    def test_plugin_is_runtime_discoverable_and_returns_registry_observations(self) -> None:
        adapter_id = "virginia_deq_nps_nutrient_credit_registry"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, VirginiaDeqNpsNutrientCreditRegistryAdapter)
        self.assertEqual(NUTRIENT_TRADING_URL, adapter.info.docs_url)
        self.assertIn("phosphorus_credit_availability", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_virginia() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.virginia_deq_nps_nutrient_credit_registry.fetch_text",
            return_value=fetch_result(),
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_virginia):
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

    def test_spec_1250_is_covered_by_the_runtime_adapter(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #1250: Virginia DEQ / NPS Nutrient Credit Registry",
                "market_key": "global_discovery|Virginia DEQ / NPS Nutrient Credit Registry",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Virginia DEQ / NPS Nutrient Credit Registry",
                        "public_docs_url": NUTRIENT_TRADING_URL,
                        "asset_or_event": "NPS nutrient banks with phosphorus credit availability",
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("virginia_deq_nps_nutrient_credit_registry", match["adapter_id"])
        self.assertIn("nutrient_credit_registry", match["available_capabilities"])


if __name__ == "__main__":
    unittest.main()
