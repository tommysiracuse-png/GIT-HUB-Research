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
from adapters.venues.banco_central_de_bolivia_bcb import (
    OMA_PUBLICATION_URL,
    BancoCentralDeBoliviaAdapter,
    parse_bcb_electronic_auction_and_repo_rules,
    parse_bcb_oma_auction_results,
    parse_bcb_oma_result_links,
)


PUBLICATION = """
<html><body><h1>Difusión de resultados de la subasta 7/2026, publicación OMA</h1>
<div class="field-name-field-archivo"><a href="/webdocs/files_noticias/subasta_12.02.26.pdf">subasta_12.02.26.pdf</a></div>
</body></html>
"""

OMA_RESULT = """
I. Adjudicaciones de Subasta de la semana 7/2026, del 11/02/2026
LB-MN BCB 91 - 38.051 6,7998
LR-MN BCB 182 91 92.798 9,4750
LR-MN BCB 273 91 67.740 9,8869
LR-MN BCB 364 91 48.864 10,4212
III. Oferta de Reportos, del 12/02/2026 al 18/02/2026
Plazo 1)
MN 11,25 11,75 Bs200.000.000 10
Diario MáximoSubasta Mesa Moneda
"""

RULES = """
<html><body><h1>CIEX N° 7/2025</h1>
<p>REQUISITOS PARA PARTICIPAR EN OPERACIONES DE MERCADO ABIERTO Y EN OPERACIONES
CON VALORES PÚBLICOS EMITIDOS CON FINES DE POLÍTICA FISCAL MEDIANTE EL SISTEMA
ELECTRÓNICO PROVISTO POR EL BANCO CENTRAL DE BOLIVIA Y PARA SUSCRIBIR EL CONTRATO
DE SUBASTA ELECTRÓNICA DE VALORES Y REPORTOS</p></body></html>
"""


def text_result(text: str, received_at: str = "2026-02-12T15:30:00+00:00") -> dict:
    return {"ok": True, "status": "reachable", "http_status": 200, "text": text, "received_at": received_at, "latency_ms": 4.0}


class BancoCentralDeBoliviaAdapterTests(unittest.TestCase):
    def test_parsers_normalize_273_day_award_repo_offer_and_rules(self) -> None:
        links = parse_bcb_oma_result_links(PUBLICATION)
        self.assertEqual(["https://www.bcb.gob.bo/webdocs/files_noticias/subasta_12.02.26.pdf"], links)

        rows = parse_bcb_oma_auction_results(
            OMA_RESULT, source_url=links[0], received_at="2026-02-12T15:30:00+00:00"
        )
        by_symbol = {row["symbol"]: row for row in rows}
        award = by_symbol["LR_MN_273D"]
        self.assertEqual(9.8869, award["last"])
        self.assertEqual(67_740.0, award["auction_awarded_quantity_thousands"])
        self.assertEqual(67_740_000.0, award["auction_awarded_nominal_bob"])
        self.assertTrue(award["paper_experiment_eligible"])
        self.assertEqual("weekly_auction_results_published", award["session_status"])

        repo = by_symbol["BCB_REPO_MN"]
        self.assertEqual(11.25, repo["repo_minimum_rate_pct"])
        self.assertEqual(11.75, repo["repo_maximum_rate_pct"])
        self.assertEqual(200_000_000.0, repo["repo_offer_maximum_amount_bob"])
        self.assertEqual(10, repo["repo_term_days"])

        rules = parse_bcb_electronic_auction_and_repo_rules(RULES, received_at="2026-02-12T15:30:00+00:00")
        self.assertEqual("CIEX_7_2025", rules[0]["symbol"])
        self.assertTrue(rules[0]["public_repo_operations_enabled"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in [*rows, *rules]))

    def test_runtime_discovery_scan_and_parser_failure_evidence(self) -> None:
        adapter_id = "banco_central_de_bolivia_bcb_oma"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, BancoCentralDeBoliviaAdapter)
        self.assertEqual(OMA_PUBLICATION_URL, adapter.info.docs_url)
        self.assertIn("public_repo_auction", adapter.info.capabilities)

        with mock.patch("adapters.venues.banco_central_de_bolivia_bcb.fetch_text", side_effect=[text_result(PUBLICATION), text_result(RULES)]), mock.patch(
            "adapters.venues.banco_central_de_bolivia_bcb.fetch_bytes", return_value=text_result(OMA_RESULT)
        ):
            batch = BancoCentralDeBoliviaAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["oma_result_1"]["fetch_status"])
        self.assertEqual(1, batch.metadata["result_document_count"])
        self.assertEqual(6, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.banco_central_de_bolivia_bcb.fetch_text", side_effect=[text_result(PUBLICATION), text_result(RULES)]
        ), mock.patch(
            "adapters.venues.banco_central_de_bolivia_bcb.fetch_bytes", return_value=text_result(OMA_RESULT)
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
        report = runtime_batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertTrue(all(row["source_adapter_id"] == adapter_id for row in runtime_batch.observations))

        with mock.patch("adapters.venues.banco_central_de_bolivia_bcb.fetch_text", side_effect=[text_result("<html>replacement</html>"), text_result(RULES)]):
            failed = BancoCentralDeBoliviaAdapter().scan({})
        self.assertEqual("degraded", failed.metadata["source_status"])
        self.assertEqual(1, len(failed.metadata["parser_failures"]))
        health = next(row for row in failed.observations if row["symbol"] == "OMA_PUBLICATION_HEALTH")
        self.assertIsNotNone(health["parser_failure"])
        self.assertEqual("watch_only", health["direction"])

    def test_capability_reconciliation_matches_spec_690(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #690: Banco Central de Bolivia (BCB)",
                "market_key": "global_discovery|Banco Central de Bolivia (BCB)",
                "spec": {"candidate": {"venue_or_source": "Banco Central de Bolivia (BCB)", "public_docs_url": OMA_PUBLICATION_URL, "data_access_type": "public_no_key"}},
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("banco_central_de_bolivia_bcb_oma", match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
