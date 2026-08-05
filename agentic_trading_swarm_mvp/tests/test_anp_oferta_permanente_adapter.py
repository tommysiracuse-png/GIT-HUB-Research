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
from adapters.venues.anp_oferta_permanente_de_concessao import (
    ANNOUNCEMENT_URL,
    BLOCKS_URL,
    AnpOfertaPermanenteDeConcessaoAdapter,
    parse_anp_opc_45_block_announcement,
    parse_anp_opc_exploratory_blocks,
)


CATALOG_TEXT = """
<html><body>
<h1>Blocos Exploratórios</h1>
<p>Oferta Permanente sob o regime de Concessão</p>
<p>Publicado em 07/07/2021 16h36 Atualizado em 16/07/2026 18h46</p>
<p>A versão vigente do Edital de Licitações da Oferta Permanente de Concessão
contempla um total de 495 blocos exploratórios disponíveis para oferta.</p>
</body></html>
"""

ANNOUNCEMENT_TEXT = """
<html><body>
<h1>Oferta Permanente de Concessão (OPC): edital com inclusão de 45 blocos
passará por audiência pública</h1>
<p>São blocos marítimos nas bacias de Campos e Santos e terrestres, na Bacia Potiguar</p>
<p>Publicado em 14/04/2026 10h41</p>
<p>A ANP realizará audiência pública no dia 28/04/2026 para tratar da atualização do
edital da Oferta Permanente de Concessão (OPC), com a inclusão de 45 novos blocos
exploratórios. São 37 marítimos (Bacias de Campos e Santos) e oito terrestres
(Bacia Potiguar).</p>
</body></html>
"""


def fetch_result(text: str, received_at: str = "2026-08-04T08:30:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class AnpOfertaPermanenteAdapterTests(unittest.TestCase):
    def test_plugin_is_discoverable_and_strictly_paper_only(self) -> None:
        adapter_id = "anp_oferta_permanente_de_concessao"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, AnpOfertaPermanenteDeConcessaoAdapter)
        self.assertEqual(BLOCKS_URL, adapter.info.docs_url)
        self.assertIn("exploration_block_catalog", adapter.info.capabilities)
        self.assertIn("public_consultation_schedule", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_capability_inventory_resolves_adapter_spec_1247_without_quote_claims(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement adapter spec #1247 for ANP / Oferta Permanente de Concessão",
                "market_key": "global_discovery|ANP / Oferta Permanente de Concessão",
                "spec": {
                    "candidate": {
                        "venue_or_source": "ANP / Oferta Permanente de Concessão",
                        "public_docs_url": BLOCKS_URL,
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )

        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("anp_oferta_permanente_de_concessao", match["adapter_id"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_parsers_normalize_current_catalogue_and_45_block_notice(self) -> None:
        catalogue = parse_anp_opc_exploratory_blocks(
            CATALOG_TEXT, received_at="2026-07-20T12:00:00+00:00"
        )
        announcement = parse_anp_opc_45_block_announcement(
            ANNOUNCEMENT_TEXT, received_at="2026-04-20T12:00:00+00:00"
        )

        self.assertEqual(1, len(catalogue))
        self.assertEqual("ANP:OPC:EXPLORATORY_BLOCKS:AVAILABLE", catalogue[0]["inst_id"])
        self.assertEqual(495, catalogue[0]["available_exploratory_blocks"])
        self.assertEqual("fresh", catalogue[0]["freshness_state"])
        self.assertEqual("available_for_continuous_offer", catalogue[0]["session_status"])
        self.assertEqual(BLOCKS_URL, catalogue[0]["source_url"])

        self.assertEqual(1, len(announcement))
        row = announcement[0]
        self.assertEqual("ANP:OPC:NEW_EXPLORATORY_BLOCKS:2026-04-14", row["inst_id"])
        self.assertEqual(45, row["new_exploratory_blocks"])
        self.assertEqual(37, row["offshore_new_blocks"])
        self.assertEqual(8, row["onshore_new_blocks"])
        self.assertEqual(("Campos", "Santos"), row["offshore_basins"])
        self.assertEqual(("Potiguar",), row["onshore_basins"])
        self.assertEqual("2026-04-28", row["public_hearing_date"])
        self.assertTrue(all(item["direction"] == "watch_only" for item in [*catalogue, *announcement]))
        self.assertTrue(all(item["price_available"] is False for item in [*catalogue, *announcement]))

    def test_adapter_retains_parser_and_fetch_health_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T08:30:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.anp_oferta_permanente_de_concessao.fetch_text",
            side_effect=[fetch_result("<html>replacement page</html>"), blocked],
        ):
            batch = AnpOfertaPermanenteDeConcessaoAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["blocks_catalog"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["45_block_announcement"]["fetch_status"])
        self.assertIn("catalogue markers", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual("public_anp_opc_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual("public_anp_opc_source_unavailable", batch.observations[1]["candidate_reject_reason"])

    def test_adapter_runtime_auto_discovers_normalized_anp_batch(self) -> None:
        adapter_id = "anp_oferta_permanente_de_concessao"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_anp() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.anp_oferta_permanente_de_concessao.fetch_text",
            side_effect=[fetch_result(CATALOG_TEXT), fetch_result(ANNOUNCEMENT_TEXT)],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_anp):
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


if __name__ == "__main__":
    unittest.main()
