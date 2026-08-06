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
from adapters.venues.indonesia_commodity_derivatives_exchange_icdx import (
    ABOUT_URL,
    CPO_URL,
    EXCHANGE_URL,
    HOME_URL,
    IndonesiaCommodityDerivativesExchangeIcdxAdapter,
    parse_icdx_about_milestones,
    parse_icdx_cpotr_reference,
    parse_icdx_exchange_surface,
    parse_icdx_homepage_price_cards,
)


EXCHANGE_PAGE = """
<html><body>
  <h2>GOFX</h2>
  <p>GOFX is a suite of futures products encompassing Gold, Oil, and Forex,
  traded on the Indonesia Commodity and Derivatives Exchange (ICDX).</p>
  <p>These transactions occur in a multilateral market supervised by the
  Indonesian government's Commodity Futures Trading Regulatory Agency (CoFTRA).</p>
  <p>GOFX is integrated with MetaTrader 5 (MT5).</p>
  <p>ICDX offers Spot Gold contracts in three sizes: 1 gram, 1 ounce, and 10 ounces.</p>
  <p>Crude Oil Futures are part of GOFX.</p>
  <p>ICDX also offers spot forex contracts.</p>
</body></html>
"""

CPO_PAGE = """
<html><body>
  <p>ICDX launched the CPOTR futures contract in 2010.</p>
  <p>This contract serves as a reference for Indonesian CPO prices.</p>
  <p>ICDX also introduced physical CPO trading through an exchange auction mechanism.</p>
</body></html>
"""

ABOUT_PAGE = """
<html><body>
  <p>2009 Indonesia Commodity &amp; Derivatives Exchange (ICDX) was established.</p>
  <p>2010 CPOTR futures contract launch as a benchmark price for CPO exporter in Indonesia.</p>
  <p>2018 The launch of GOFX as the first regulated exchange-traded rolling spot and futures platform in ASEAN.</p>
  <p>2019 The launch of GOFX Micro.</p>
  <p>2020 The launch of COFR, COFU10, and COFU100 contracts.</p>
</body></html>
"""

HOME_PAGE = """
<html><body>
  <div>OFFICIAL PRICES</div>
  <div>SOBO CPOTR AUG26</div>
  <div>(Suggested Opening)</div>
  <div>16875</div>
  <div>YDSP CPOTR AUG26</div>
  <div>(Previous Settlement)</div>
  <div>16280</div>
</body></html>
"""


def fetch_result(text: str, received_at: str = "2026-08-06T12:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class IndonesiaCommodityDerivativesExchangeIcdxAdapterTests(unittest.TestCase):
    def test_plugin_is_discoverable_and_paper_only(self) -> None:
        adapter_id = "indonesia_commodity_derivatives_exchange_icdx"

        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)

        self.assertIsInstance(adapter, IndonesiaCommodityDerivativesExchangeIcdxAdapter)
        self.assertEqual(EXCHANGE_URL, adapter.info.docs_url)
        self.assertIn("official_price_card", adapter.info.capabilities)
        self.assertNotIn("order_entry", adapter.info.capabilities)

    def test_parsers_normalize_gofx_surfaces_cpotr_reference_and_price_cards(self) -> None:
        exchange_rows = parse_icdx_exchange_surface(EXCHANGE_PAGE, received_at="2026-08-06T12:00:00+00:00")
        cpotr_rows = parse_icdx_cpotr_reference(CPO_PAGE, received_at="2026-08-06T12:00:00+00:00")
        milestone_rows = parse_icdx_about_milestones(ABOUT_PAGE, received_at="2026-08-06T12:00:00+00:00")
        price_rows = parse_icdx_homepage_price_cards(HOME_PAGE, received_at="2026-08-06T12:00:00+00:00")

        self.assertEqual(3, len(exchange_rows))
        self.assertEqual("ICDX:GOFX:GOLD", exchange_rows[0]["inst_id"])
        self.assertEqual("CoFTRA", exchange_rows[0]["regulator"])
        self.assertEqual("MetaTrader 5", exchange_rows[0]["platform"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in exchange_rows))
        self.assertTrue(all(row["source_url"] == EXCHANGE_URL for row in exchange_rows))

        self.assertEqual(1, len(cpotr_rows))
        self.assertEqual("ICDX:CPOTR:REFERENCE", cpotr_rows[0]["inst_id"])
        self.assertEqual(2010, cpotr_rows[0]["benchmark_since_year"])
        self.assertEqual(CPO_URL, cpotr_rows[0]["source_url"])

        self.assertEqual(1, len(milestone_rows))
        self.assertEqual(2018, milestone_rows[0]["gofx_launch_year"])
        self.assertEqual(["COFR", "COFU10", "COFU100"], milestone_rows[0]["crude_oil_contract_codes"])
        self.assertEqual(ABOUT_URL, milestone_rows[0]["source_url"])

        self.assertEqual(2, len(price_rows))
        suggested, settlement = price_rows
        self.assertEqual("ICDX:CPOTR:AUG26:SOBO", suggested["inst_id"])
        self.assertEqual(16875.0, suggested["last"])
        self.assertEqual("pre_open_indicative", suggested["session_status"])
        self.assertEqual("ICDX:CPOTR:AUG26:YDSP", settlement["inst_id"])
        self.assertEqual(16280.0, settlement["last"])
        self.assertEqual("previous_settlement_reference", settlement["session_status"])
        self.assertTrue(all(row["source_url"] == HOME_URL for row in price_rows))

    def test_adapter_preserves_fetch_and_parser_failure_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-06T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.indonesia_commodity_derivatives_exchange_icdx.fetch_text",
            side_effect=[
                fetch_result(HOME_PAGE),
                fetch_result("<html><body>replacement page</body></html>"),
                blocked,
                fetch_result(ABOUT_PAGE),
            ],
        ):
            batch = IndonesiaCommodityDerivativesExchangeIcdxAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["homepage"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["exchange"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["cpo_physical_market"]["fetch_status"])
        self.assertIn("exchange parser failed", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(3, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row["candidate_reject_reason"] == "public_icdx_parser_failure" for row in batch.observations))
        self.assertTrue(any(row["candidate_reject_reason"] == "public_icdx_source_unavailable" for row in batch.observations))

    def test_adapter_runtime_auto_discovers_normalized_batch(self) -> None:
        adapter_id = "indonesia_commodity_derivatives_exchange_icdx"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_icdx() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.indonesia_commodity_derivatives_exchange_icdx.fetch_text",
            side_effect=[
                fetch_result(HOME_PAGE),
                fetch_result(EXCHANGE_PAGE),
                fetch_result(CPO_PAGE),
                fetch_result(ABOUT_PAGE),
            ],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_icdx):
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
        self.assertEqual(7, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(456, report["adapters"][0]["adapter_spec_id"])


if __name__ == "__main__":
    unittest.main()
