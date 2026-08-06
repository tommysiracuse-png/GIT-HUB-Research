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

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.pronovo_ag import (
    HYDROGEN_REQUIREMENTS_URL,
    IMPORT_GAS_URL,
    SOURCE_URL,
    PronovoAgBtGuaranteesOfOriginAdapter,
    parse_pronovo_bt_overview,
    parse_pronovo_gas_import_routes,
    parse_pronovo_hydrogen_requirements,
)


OVERVIEW = """
<html><body>
  <h1>Herkunftsnachweise (HKN) fuer erneuerbare Brenn- und Treibstoffe (BT)</h1>
  <p>Die von Pronovo ausgestellten Nachweise garantieren die Herkunft der in der
  Schweiz gehandelten biogenen Treib- und Brennstoffe.</p>
  <p>Seit dem 1. Januar 2025 besteht die gesetzliche Pflicht, dass die
  schweizerische Produktion sowie der Import von erneuerbaren Treib- und
  Brennstoffen mittels Herkunftsnachweisen in einem Herkunftsnachweissystem
  erfasst werden muessen.</p>
  <p>Pronovo errichtet mit dem BAFU und dem BFE ein nationales
  Herkunftsnachweissystem fuer erneuerbare gasfoermige und fluessige Brenn-
  und Treibstoffe.</p>
</body></html>
"""

IMPORT_GAS = """
<html><body>
  <h1>Import von Gas-HKN</h1>
  <p>Pronovo ist seit Anfang 2025 Mitglied des European Renewable Gas Registry
  (ERGaR) sowie seit Juni 2025 Mitglied der Gas Scheme Group (GSG) der
  Association of Issuing Bodies (AIB).</p>
  <h2>Import von EECS-Zertifikaten ueber den AIB-Hub</h2>
  <ul>
    <li>Oesterreich (E-Control)</li>
    <li>Spanien (Enagas GTS)</li>
    <li>Slowakei (SPP Distribucia)</li>
  </ul>
  <h2>Import von CoO ueber den ERGaR-Hub</h2>
  <ul>
    <li>Deutschland (Dena Bioregister)</li>
    <li>England (GGCS)</li>
    <li>Daenemark (Energinet)</li>
  </ul>
</body></html>
"""

H2_REQUIREMENTS = """
<html><body>
  <h1>Welche Anforderungen gelten fuer H2-Zertifikate? Ist eine Anrechnung moeglich?</h1>
  <p>Die Verordnung laesst den Import von auslaendischen Zertifikaten fuer
  erneuerbare Gase nur fuer Mengen zu, die ins europaeische Gasnetz
  eingespeist wurden.</p>
  <p>Es wird eine Positivliste mit Zertifizierungssystemen geben. Die Liste ist
  noch nicht publiziert. Sie wird bis Ende Jahr veroeffentlicht.</p>
  <p>Wenn die Mengen physisch in die Schweiz importiert werden, gelten die
  oekologischen Anforderungen gemaess des revidierten Art. 35d des
  Umweltschutzgesetzes.</p>
</body></html>
"""


def text_result(text: str, received_at: str = "2026-08-06T10:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class PronovoAgAdapterTests(unittest.TestCase):
    def test_parsers_normalize_registry_import_and_hydrogen_guidance(self) -> None:
        overview_rows = parse_pronovo_bt_overview(
            OVERVIEW, received_at="2026-08-06T10:00:00+00:00"
        )
        self.assertEqual(2, len(overview_rows))
        gas, liquid = overview_rows
        self.assertEqual("PRONOVO_AG:BT:REGISTRY:RENEWABLE_GAS", gas["inst_id"])
        self.assertEqual("renewable_gas", gas["fuel_family"])
        self.assertTrue(gas["import_tracking_required"])
        self.assertEqual(SOURCE_URL, gas["source_url"])
        self.assertEqual("watch_only", gas["direction"])
        self.assertEqual("LIQUID_BIOFUEL", liquid["base"])
        self.assertEqual("liquid_biofuel", liquid["fuel_family"])

        import_rows = parse_pronovo_gas_import_routes(
            IMPORT_GAS, received_at="2026-08-06T10:05:00+00:00"
        )
        by_symbol = {row["symbol"]: row for row in import_rows}
        self.assertEqual(2, len(import_rows))
        self.assertEqual(3, by_symbol["CH_BT_GAS_IMPORT_AIB"]["connected_jurisdiction_count"])
        self.assertIn(
            "Spanien (Enagas GTS)",
            by_symbol["CH_BT_GAS_IMPORT_AIB"]["connected_jurisdictions"],
        )
        self.assertEqual("ERGaR", by_symbol["CH_BT_GAS_IMPORT_ERGAR"]["import_hub"])
        self.assertEqual(IMPORT_GAS_URL, by_symbol["CH_BT_GAS_IMPORT_ERGAR"]["source_url"])

        hydrogen = parse_pronovo_hydrogen_requirements(
            H2_REQUIREMENTS, received_at="2026-08-06T10:10:00+00:00"
        )[0]
        self.assertEqual("PRONOVO_AG:BT:H2_IMPORT:REQUIREMENTS", hydrogen["inst_id"])
        self.assertTrue(hydrogen["eligible_only_if_injected_into_european_gas_grid"])
        self.assertEqual("pending_publication", hydrogen["positive_list_status"])
        self.assertEqual("USG_35D", hydrogen["physical_import_rule_reference"])
        self.assertEqual(HYDROGEN_REQUIREMENTS_URL, hydrogen["source_url"])

    def test_scan_preserves_real_rows_and_watch_only_health_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-06T10:05:00+00:00",
            "latency_ms": 6.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.pronovo_ag.fetch_text",
            side_effect=[
                text_result(OVERVIEW),
                blocked,
                text_result("<html><body>replacement page</body></html>"),
            ],
        ):
            batch = PronovoAgBtGuaranteesOfOriginAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["overview"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["gas_import"]["fetch_status"])
        self.assertEqual(
            "reachable",
            batch.metadata["fetch_status"]["hydrogen_requirements"]["fetch_status"],
        )
        self.assertEqual(2, batch.metadata["real_observation_count"])
        self.assertEqual(1, len(batch.metadata["parser_failures"]))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("quality_status") == "source_health" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))

    def test_plugin_runtime_discovery_and_spec_reconciliation(self) -> None:
        adapter_id = "pronovo_ag_bt_guarantees_of_origin"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, PronovoAgBtGuaranteesOfOriginAdapter)
        self.assertEqual(SOURCE_URL, adapter.info.docs_url)
        self.assertIn("gas_certificate_import_routes", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_pronovo() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.pronovo_ag.fetch_text",
            side_effect=[text_result(OVERVIEW), text_result(IMPORT_GAS), text_result(H2_REQUIREMENTS)],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_pronovo):
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
        self.assertEqual(5, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #1479: Pronovo AG",
                "market_key": "global_discovery|Pronovo AG",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Pronovo AG",
                        "public_docs_url": SOURCE_URL,
                        "asset_or_event": (
                            "Swiss guarantees of origin for renewable thermal and motor fuels "
                            "(BT system), including gas, liquid biofuels, and hydrogen-related entries"
                        ),
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
