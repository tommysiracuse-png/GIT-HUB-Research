from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from adapters.registry import discover_adapters, get_adapter
import adapter_capabilities
from adapters.venues.casablanca_stock_exchange_derivatives_market import (
    CONTRACT_SYMBOLS,
    SOURCE_URL,
    CasablancaStockExchangeDerivativesMarketAdapter,
    parse_casablanca_derivatives_market,
)


def _instruments_fixture() -> str:
    return """
    <html><body>
      <p>Session closed Monday, June 16, 2026</p>
      <p>Prices are delayed by 15 minutes.</p>
      <table>
        <tr><th>Ticker</th><th>Contract</th><th>Price</th><th>Previous closing price</th><th>Change</th><th>Quantity traded</th></tr>
        <tr><td>FMASI20JUI26</td><td>MASI 20 Future JUI26</td><td>1 320,00</td><td>1 310,00</td><td>0,76 %</td><td>12</td></tr>
        <tr><td>FMASI20SEP26</td><td>MASI 20 Future SEP26</td><td>1 325,50</td><td>1 320,00</td><td>0,42 %</td><td>8</td></tr>
        <tr><td>FMASI20DEC26</td><td>MASI 20 Future DEC26</td><td>-</td><td>1 330,00</td><td>-</td><td>-</td></tr>
        <tr><td>FMASI20MAR27</td><td>MASI 20 Future MAR27</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>
      </table>
    </body></html>
    """


class CasablancaStockExchangeDerivativesMarketAdapterTests(unittest.TestCase):
    def test_parser_normalizes_all_requested_contracts_with_public_provenance(self) -> None:
        rows = parse_casablanca_derivatives_market(
            _instruments_fixture(), received_at="2026-06-16T16:00:00+00:00"
        )

        self.assertEqual(list(CONTRACT_SYMBOLS), [row["symbol"] for row in rows])
        self.assertEqual("CASABLANCA_DERIVATIVES:FMASI20JUI26", rows[0]["inst_id"])
        self.assertEqual(1320.0, rows[0]["last"])
        self.assertEqual(0.76, rows[0]["change_pct"])
        self.assertEqual(12.0, rows[0]["quantity_traded"])
        self.assertEqual(1330.0, rows[2]["last"])
        self.assertEqual("previous_close", rows[2]["price_basis"])
        self.assertEqual(0.0, rows[3]["last"])
        self.assertEqual("contract_identity_only", rows[3]["price_basis"])
        for row in rows:
            self.assertEqual("watch_only", row["direction"])
            self.assertEqual("reachable", row["fetch_status"])
            self.assertEqual("fresh", row["freshness_state"])
            self.assertEqual("closed", row["session_status"])
            self.assertEqual(SOURCE_URL, row["source_url"])

    def test_runtime_discovery_and_failures_remain_paper_only(self) -> None:
        adapter_id = "casablanca_stock_exchange_derivatives_market"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, CasablancaStockExchangeDerivativesMarketAdapter)
        self.assertEqual(SOURCE_URL, adapter.info.docs_url)
        self.assertNotIn("entry_quality_quote", adapter.info.capabilities)

        reachable = {
            "ok": True, "status": "reachable", "http_status": 200,
            "text": _instruments_fixture(), "received_at": "2026-06-16T16:00:00+00:00", "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_derivatives_market.fetch_text",
            return_value=reachable,
        ):
            batch = adapter.scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual(500, batch.metadata["adapter_spec_id"])
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(list(CONTRACT_SYMBOLS), batch.metadata["observed_contracts"])
        self.assertTrue(batch.metadata["paper_only"])

        blocked = {"ok": False, "status": "blocked", "http_status": 403, "error": "blocked", "received_at": "2026-06-16T16:01:00+00:00"}
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_derivatives_market.fetch_text",
            return_value=blocked,
        ):
            unavailable = adapter.scan({})
        self.assertEqual("blocked", unavailable.metadata["source_status"])
        self.assertEqual("watch_only", unavailable.observations[0]["direction"])
        self.assertEqual("blocked", unavailable.observations[0]["fetch_status"])

        malformed = {**reachable, "text": "<html><body>replacement page</body></html>"}
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_derivatives_market.fetch_text",
            return_value=malformed,
        ):
            degraded = adapter.scan({})
        self.assertEqual("degraded", degraded.metadata["source_status"])
        self.assertTrue(degraded.metadata["parser_failures"])
        self.assertEqual("reachable", degraded.metadata["fetch_status"]["instruments"]["fetch_status"])
        self.assertTrue(degraded.observations[0]["parser_failure"])

    def test_adapter_capability_reconciliation_matches_spec_500(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #500: Casablanca Stock Exchange / Derivatives Market",
                "market_key": "global_discovery|Casablanca Stock Exchange / Derivatives Market",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Casablanca Stock Exchange / Derivatives Market",
                        "public_docs_url": SOURCE_URL,
                        "asset_or_event": "MASI 20 index futures contract FMASI20JUI26 / FMASI20SEP26 / FMASI20DEC26 / FMASI20MAR27",
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("casablanca_stock_exchange_derivatives_market", match["adapter_id"])
        self.assertIn("delayed_quote", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])


if __name__ == "__main__":
    unittest.main()
