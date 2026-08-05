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
from adapters.venues.california_air_resources_board_quebec_ministry import (
    NOTICE_REPORTS_URL,
    PRINTABLE_RESULT_URL,
    CaliforniaAirResourcesBoardQuebecMinistryAdapter,
    parse_california_quebec_joint_auction,
)


def _result_fixture() -> str:
    return """
    <html><body>
      <h1>California and Quebec release summary results of 47th joint cap-and-invest allowance auction</h1>
      <p>Auction Date: May 21, 2025</p>
      <h2>Current Auction</h2>
      <p>Current Auction Settlement Price: $25.87</p>
      <p>Current Auction Allowances Offered: 51,177,593</p>
      <p>2025 Vintage Allowances</p>
      <h2>Advance Auction</h2>
      <p>Advance Auction Settlement Price: $25.22</p>
      <p>Advance Auction Allowances Offered: 7,500,000</p>
      <p>2028 Vintage Allowances</p>
    </body></html>
    """


class CaliforniaAirResourcesBoardQuebecAdapterTests(unittest.TestCase):
    def test_parser_normalizes_current_and_advance_auction_results(self) -> None:
        rows = parse_california_quebec_joint_auction(
            _result_fixture(),
            received_at="2025-05-22T00:00:00+00:00",
        )

        self.assertEqual(2, len(rows))
        current, advance = rows
        self.assertEqual("CARB_QUEBEC:JOINT_AUCTION:47:CURRENT", current["inst_id"])
        self.assertEqual(25.87, current["auction_settlement_price_usd"])
        self.assertEqual(51_177_593.0, current["allowances_offered"])
        self.assertEqual(2025, current["vintage_year"])
        self.assertEqual("closed", current["session_status"])
        self.assertEqual(25.22, advance["last"])
        self.assertEqual(2028, advance["vintage_year"])
        self.assertEqual(PRINTABLE_RESULT_URL, current["source_url"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["candidate_reject_reason"] for row in rows))

    def test_parser_normalizes_notice_reserve_prices_as_scheduled_supply(self) -> None:
        notice = """
        <html><body>
          <h1>California and Quebec announce 48th joint cap-and-invest allowance auction</h1>
          <p>Auction will be held on August 20, 2025.</p>
          <h2>Current Auction</h2><p>Current Auction Reserve Price: $26.00</p>
          <p>Current Auction Allowances Available for Sale: 50,000,000</p>
          <p>2025 Vintage Allowances</p>
          <h2>Advance Auction</h2><p>Advance Auction Reserve Price: $26.00</p>
          <p>Advance Auction Allowances Available for Sale: 7,000,000</p>
          <p>2028 Vintage Allowances</p>
        </body></html>
        """
        rows = parse_california_quebec_joint_auction(
            notice,
            received_at="2025-07-01T00:00:00+00:00",
        )

        self.assertEqual(["scheduled", "scheduled"], [row["session_status"] for row in rows])
        self.assertEqual([26.0, 26.0], [row["auction_reserve_price_usd"] for row in rows])
        self.assertEqual([50_000_000.0, 7_000_000.0], [row["allowances_offered"] for row in rows])
        self.assertTrue(all(row["auction_settlement_price_usd"] is None for row in rows))

    def test_plugin_is_runtime_discoverable_and_preserves_source_failures(self) -> None:
        adapter_id = "california_air_resources_board_qu_bec_ministry"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, CaliforniaAirResourcesBoardQuebecMinistryAdapter)
        self.assertEqual(NOTICE_REPORTS_URL, adapter.info.docs_url)
        self.assertIn("advance_vintage", adapter.info.capabilities)

        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": _result_fixture(),
            "received_at": "2025-05-22T00:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.california_air_resources_board_quebec_ministry.fetch_text",
            return_value=reachable,
        ):
            batch = CaliforniaAirResourcesBoardQuebecMinistryAdapter().scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("closed", batch.metadata["session_state"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["joint_auction_result"]["fetch_status"])
        self.assertEqual(2, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])

        malformed = {**reachable, "text": "<html><body>replacement page</body></html>"}
        with mock.patch(
            "adapters.venues.california_air_resources_board_quebec_ministry.fetch_text",
            return_value=malformed,
        ):
            parser_batch = CaliforniaAirResourcesBoardQuebecMinistryAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("parser_failure", parser_batch.observations[0]["freshness_basis"])
        self.assertEqual("public_allowance_auction_parser_failure", parser_batch.observations[0]["candidate_reject_reason"])

        blocked = {**reachable, "ok": False, "status": "blocked", "http_status": 403, "text": ""}
        with mock.patch(
            "adapters.venues.california_air_resources_board_quebec_ministry.fetch_text",
            return_value=blocked,
        ):
            unavailable_batch = CaliforniaAirResourcesBoardQuebecMinistryAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(PRINTABLE_RESULT_URL, unavailable_batch.observations[0]["source_url"])


if __name__ == "__main__":
    unittest.main()
