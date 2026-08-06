from __future__ import annotations

import json
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
from adapters.venues.republican_stock_exchange_toshkent import (
    SECURITY_HISTORY_URL,
    SPLITS_URL,
    TRADE_RESULTS_URL,
    RepublicanStockExchangeToshkentAdapter,
    parse_uzse_security_history,
    parse_uzse_splits,
    parse_uzse_trade_results,
)


TRADE_RESULTS_HTML = """
<html><body><table>
  <thead><tr>
    <th>Time</th><th>Logo</th><th>SEC CODE</th><th>Isu Name</th>
    <th>Security type</th><th>Market</th><th>BRD ID</th>
    <th>Trade Price</th><th>Quantity</th><th>Trading Value</th>
  </tr></thead>
  <tbody>
    <tr><td>August 4, 2026 16:02</td><td></td><td>UZ7057480012 TMYS</td>
      <td>TEMIRYOL SUGURTA AJ</td><td>Common stocks</td><td>STK</td><td>G1</td>
      <td>1,500</td><td>4</td><td>UZS 6,000</td></tr>
    <tr><td>August 4, 2026 15:00</td><td></td><td>UZ7000000001 NEGO</td>
      <td>Negotiated Block AJ</td><td>Common stocks</td><td>STK</td><td>T1</td>
      <td>2,500.50</td><td>100,000</td><td>UZS 250,050,000</td></tr>
    <tr><td>August 4, 2026 14:00</td><td></td><td>UZ7000000002 FOPX</td>
      <td>Free Delivery AJ</td><td>Common stocks</td><td>STK</td><td>NC</td>
      <td>950</td><td>10,000</td><td>UZS 9,500,000</td></tr>
  </tbody>
</table></body></html>
"""


SECURITY_HISTORY_HTML = """
<html><body>
  <table><tr><td>
    UZ6058027AB0 DMMT2B DELTA MIKROMOLIYA TASHKILOTI
    Last transaction 04.08.2026 1,050,000
  </td></tr></table>
  <table>
    <tr><th>Time</th><th>Price</th><th>Change</th><th>Quantity</th>
      <th>Trading Value(UZS)</th></tr>
    <tr><td>10:25:01</td><td>1,050,000</td><td>0</td><td>9</td><td>9,450,000</td></tr>
  </table>
  <table>
    <tr><th>Date</th><th>Closed Price</th><th>Change</th><th>Quantity</th>
      <th>Trading Value(UZS)</th><th>Splits Applied</th></tr>
    <tr><td>2026-08-03</td><td>525,000</td><td>0</td><td>16</td>
      <td>8,400,000</td><td>2:1</td></tr>
  </table>
</body></html>
"""


def fetch_result(text: str, status: str = "reachable", ok: bool = True) -> dict:
    return {
        "ok": ok,
        "status": status,
        "http_status": 200 if ok else 503,
        "text": text,
        "received_at": "2026-08-04T12:00:00+00:00",
        "latency_ms": 4.5,
        "error": None if ok else "source unavailable",
    }


class RepublicanStockExchangeToshkentParserTests(unittest.TestCase):
    def test_trade_results_normalize_main_nego_and_fop_boards(self) -> None:
        rows = parse_uzse_trade_results(
            TRADE_RESULTS_HTML,
            received_at="2026-08-04T12:00:00+00:00",
        )
        by_board = {row["board_id"]: row for row in rows}

        self.assertEqual({"G1", "T1", "NC"}, set(by_board))
        self.assertEqual("Main Board", by_board["G1"]["board_name"])
        self.assertEqual("uzse_nego_board_trade_results", by_board["T1"]["market_surface"])
        self.assertEqual("FoP Board", by_board["NC"]["board_name"])
        self.assertTrue(
            all(row["activation_market_surface"] == "uzse_board_trade_results" for row in rows)
        )
        self.assertEqual(1_500.0, by_board["G1"]["last"])
        self.assertEqual(250_050_000.0, by_board["T1"]["trade_value_uzs"])
        self.assertEqual("fresh", by_board["G1"]["freshness_state"])
        self.assertEqual(TRADE_RESULTS_URL, by_board["G1"]["source_url"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))

    def test_security_history_includes_split_adjusted_daily_rows(self) -> None:
        rows = parse_uzse_security_history(
            SECURITY_HISTORY_HTML,
            received_at="2026-08-04T12:00:00+00:00",
        )
        by_surface = {row["market_surface"]: row for row in rows}

        intraday = by_surface["uzse_security_trade_history"]
        adjusted = by_surface["uzse_split_adjusted_security_history"]
        self.assertEqual("2026-08-04T10:25:01+05:00", intraday["observed_at"])
        self.assertEqual("UZ6058027AB0", intraday["security_code"])
        self.assertEqual("uzse_security_trade_history", intraday["activation_market_surface"])
        self.assertEqual("uzse_security_trade_history", adjusted["activation_market_surface"])
        self.assertEqual(525_000.0, adjusted["closed_price"])
        self.assertEqual("2:1", adjusted["splits_applied"])
        self.assertEqual(SECURITY_HISTORY_URL, adjusted["source_url"])

    def test_split_endpoint_normalizes_official_corporate_action(self) -> None:
        rows = parse_uzse_splits(
            [{"id": 17, "split_date": "2026-07-01", "split": "2:1"}],
            received_at="2026-08-04T12:00:00+00:00",
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(2.0, rows[0]["split_ratio"])
        self.assertEqual("2026-07-01", rows[0]["split_effective_date"])
        self.assertEqual(SPLITS_URL, rows[0]["source_url"])
        self.assertEqual("watch_only", rows[0]["direction"])
        self.assertEqual(0.0, rows[0]["last"])


class RepublicanStockExchangeToshkentRuntimeTests(unittest.TestCase):
    def test_plugin_is_runtime_discoverable_and_has_no_execution_capability(self) -> None:
        adapter_id = "republican_stock_exchange_toshkent_public"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("UZSE", adapter.info.venue)
        self.assertIn("board_trade_results", adapter.info.capabilities)
        self.assertIn("split_adjustments", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)
        self.assertNotIn("order_execution", adapter.info.capabilities)

    def test_capability_inventory_resolves_trade_result_spec_without_live_quote_claim(self) -> None:
        spec = {
            "title": "Republican Stock Exchange Toshkent board trade results and split history",
            "market_key": "global_discovery|Republican Stock Exchange Toshkent",
            "spec": {
                "candidate": {
                    "venue_or_source": "Republican Stock Exchange Toshkent",
                    "public_docs_url": "https://uzse.uz/trade_results/",
                    "why_interesting": "Main Board, Nego Board, and FoP Board completed trades",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("republican_stock_exchange_toshkent_public", match["adapter_id"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_adapter_returns_scan_batch_with_per_source_evidence(self) -> None:
        with mock.patch(
            "adapters.venues.republican_stock_exchange_toshkent.fetch_text",
            side_effect=[
                fetch_result(TRADE_RESULTS_HTML),
                fetch_result(SECURITY_HISTORY_HTML),
                fetch_result(json.dumps([])),
            ],
        ):
            batch = RepublicanStockExchangeToshkentAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["trade_results"]["fetch_status"])
        self.assertEqual(1, batch.metadata["board_observation_count"]["Nego Board"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

    def test_adapter_preserves_parser_failure_and_unavailable_source(self) -> None:
        with mock.patch(
            "adapters.venues.republican_stock_exchange_toshkent.fetch_text",
            side_effect=[
                fetch_result("<html>schema changed</html>"),
                fetch_result(SECURITY_HISTORY_HTML),
                fetch_result("", status="unavailable", ok=False),
            ],
        ):
            batch = RepublicanStockExchangeToshkentAdapter().scan({})

        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertIn("board identifier", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(
            "unavailable",
            batch.metadata["fetch_status"]["split_adjustments"]["fetch_status"],
        )
        health = [row for row in batch.observations if row.get("parser_failure")]
        self.assertEqual("public_reference_parser_failure", health[0]["candidate_reject_reason"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))


if __name__ == "__main__":
    unittest.main()
