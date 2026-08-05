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
from adapters.venues.enagas_gts import (
    SOURCE_URL,
    EnagasGtsRenewableGasGuaranteesOfOriginAdapter,
    parse_enagas_gts_guarantees_of_origin,
)


GDO_PAGE = """
<html><body>
  <h1>Guarantees of origin</h1>
  <p>Enagás GTS manages the guarantees of origin for renewable gases in Spain.</p>
  <p>A guarantee of origin is an electronic document that certifies the
  renewable character of 1 MWh of gas.</p>
  <p>The system allows for the issuance, transfer, import and export and
  cancellation of guarantees of origin.</p>
</body></html>
"""


def fetch_result(text: str) -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": "2026-08-05T12:30:00+00:00",
        "latency_ms": 4.0,
    }


class EnagasGtsAdapterTests(unittest.TestCase):
    def test_parser_normalizes_requested_renewable_gas_certificate_classes(self) -> None:
        rows = parse_enagas_gts_guarantees_of_origin(GDO_PAGE)

        self.assertEqual(4, len(rows))
        self.assertEqual(
            {
                "biomethane",
                "biogas",
                "renewable_hydrogen",
                "bio_lng",
            },
            {row["gas_type"] for row in rows},
        )
        self.assertEqual(
            {
                "gas_system_injection",
                "self_consumption",
                "off_grid",
            },
            {row["logistics_class"] for row in rows},
        )
        hydrogen = next(row for row in rows if row["gas_type"] == "renewable_hydrogen")
        self.assertEqual("ENAGAS_GTS:GDO:RENEWABLE_HYDROGEN_OFF_GRID", hydrogen["inst_id"])
        self.assertEqual(1.0, hydrogen["certificate_unit_mwh"])
        self.assertEqual("registry_reference", hydrogen["session_status"])
        self.assertEqual("fresh", hydrogen["freshness_state"])
        self.assertEqual(SOURCE_URL, hydrogen["source_url"])
        self.assertEqual("watch_only", hydrogen["direction"])
        self.assertEqual(0.0, hydrogen["last"])

    def test_scan_preserves_parser_and_unavailable_source_evidence(self) -> None:
        malformed = fetch_result("<html><body>replacement page</body></html>")
        with mock.patch("adapters.venues.enagas_gts.fetch_text", return_value=malformed):
            parser_batch = EnagasGtsRenewableGasGuaranteesOfOriginAdapter().scan({})

        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable",
            parser_batch.metadata["fetch_status"]["guarantees_of_origin"]["fetch_status"],
        )
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_renewable_gas_go_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-05T12:31:00+00:00",
            "latency_ms": 7.0,
            "error": "blocked",
        }
        with mock.patch("adapters.venues.enagas_gts.fetch_text", return_value=unavailable):
            unavailable_batch = EnagasGtsRenewableGasGuaranteesOfOriginAdapter().scan({})

        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_renewable_gas_go_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_plugin_is_runtime_discoverable_and_reports_real_observations(self) -> None:
        adapter_id = "enagas_gts_renewable_gas_guarantees_of_origin"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, EnagasGtsRenewableGasGuaranteesOfOriginAdapter)
        self.assertEqual(SOURCE_URL, adapter.info.docs_url)
        self.assertIn("off_grid_hydrogen", adapter.info.capabilities)
        self.assertIn("bio_lng", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_enagas() -> list[str]:
            return [candidate_id for candidate_id in original_discover() if candidate_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.enagas_gts.fetch_text", return_value=fetch_result(GDO_PAGE)
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_enagas):
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
        self.assertEqual(4, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])


if __name__ == "__main__":
    unittest.main()
