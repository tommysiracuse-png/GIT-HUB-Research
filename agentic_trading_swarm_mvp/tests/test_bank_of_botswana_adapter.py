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
from adapters.venues.bank_of_botswana import (
    FRAMEWORK_URL,
    MPC_DECISION_URL,
    BankOfBotswanaAdapter,
    parse_bank_of_botswana_monetary_policy_framework,
    parse_bank_of_botswana_mpc_decision,
)


FRAMEWORK = """
<html><body>
  <h1>Monetary Policy Implementation Framework</h1>
  <p>The Bank's primary instruments for open market operations are Bank of
  Botswana Certificates (BoBCs), which are used to manage liquidity and
  influence short-term market interest rates. The Bank currently issues 7-day
  and 1-month BoBCs.</p>
  <p>At its meeting of 30 April 2026, the MPC increased the MoPR by 200 basis
  points from 3.5 percent to 5.5 percent. Consequently, the SDF rate is set at
  4.5 percent, while the SCF rate is set at 6.5 percent.</p>
  <p>7-day BoBCs are auctioned weekly (Tuesdays) on a Fixed Rate Full Allotment
  basis at the MoPR, with T+1 settlement.</p>
  <p>1-month BoBCs are auctioned on the third Tuesday of each month, for a
  predetermined volume on a multiple-price basis, with T+1 settlement.</p>
  <p>Standing Deposit Facility (SDF): Overnight deposits at MoPR less 100 basis
  points, with T+0 settlement. The SDF is accessible until 18:15.</p>
  <p>Standing Credit Facility (SCF): Overnight lending at MoPR plus 100 basis
  points, with T+0 settlement, conducted on a repo basis. The SCF is available
  during BISS operating hours up to 17:30.</p>
</body></html>
"""

MPC_DECISION = """
Press Release
Monetary Policy Committee Meets
19 June 2025
Monetary Policy Rate maintained at 1.9 percent
At the meeting held on 19 June 2025, the Monetary Policy Committee (MPC) of
the Bank of Botswana maintained the Monetary Policy Rate (MoPR) at 1.9 percent.
DECISION
(a) maintain the MoPR at 1.9 percent;
(b) the 7-day Bank of Botswana Certificates auctions, repos and reverse repos
will be conducted at the MoPR of 1.9 percent;
(c) the repo tenure will be increased from 7 days to up to one month;
(d) the Standing Deposit Facility (SDF) rate is maintained at 0.9 percent,
100 basis points below the MoPR; and
(e) the Standing Credit Facility (SCF) rate is maintained at 2.9 percent,
100 basis points above the MoPR.
"""


def text_result(text: str, received_at: str = "2026-08-04T08:30:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class BankOfBotswanaAdapterTests(unittest.TestCase):
    def test_parsers_normalize_bobc_corridor_and_mpc_snapshot(self) -> None:
        rows = parse_bank_of_botswana_monetary_policy_framework(
            FRAMEWORK, received_at="2026-08-04T08:30:00+00:00"
        )
        by_symbol = {row["symbol"]: row for row in rows}

        self.assertEqual(4, len(rows))
        self.assertEqual(5.5, by_symbol["BOBC_7D"]["rate_pct"])
        self.assertEqual("fixed_rate_full_allotment", by_symbol["BOBC_7D"]["auction_method"])
        self.assertIsNone(by_symbol["BOBC_1M"]["last"])
        self.assertEqual(5.5, by_symbol["BOBC_1M"]["reference_policy_rate_pct"])
        self.assertEqual("third_tuesday_monthly", by_symbol["BOBC_1M"]["auction_frequency"])
        self.assertEqual(4.5, by_symbol["SDF_OVERNIGHT"]["rate_pct"])
        self.assertEqual(-100, by_symbol["SDF_OVERNIGHT"]["rate_spread_to_mopr_bps"])
        self.assertEqual(6.5, by_symbol["SCF_OVERNIGHT"]["rate_pct"])
        self.assertEqual("repo", by_symbol["SCF_OVERNIGHT"]["transaction_basis"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(by_symbol["BOBC_7D"]["paper_experiment_eligible"])
        self.assertFalse(by_symbol["BOBC_1M"]["paper_experiment_eligible"])
        self.assertTrue(all("candidate_reject_reason" not in row for row in rows))
        self.assertTrue(all(row["source_url"] == FRAMEWORK_URL for row in rows))

        decision = parse_bank_of_botswana_mpc_decision(
            MPC_DECISION, received_at="2025-06-20T08:30:00+00:00"
        )[0]
        self.assertEqual("BANK_OF_BOTSWANA:MPC_DECISION_2025-06-19", decision["inst_id"])
        self.assertEqual(1.9, decision["mopr_pct"])
        self.assertEqual(0.9, decision["sdf_pct"])
        self.assertEqual(2.9, decision["scf_pct"])
        self.assertEqual(MPC_DECISION_URL, decision["source_url"])

    def test_scan_retains_fetch_status_and_parser_failure_watch_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "content": b"",
            "received_at": "2026-08-04T08:30:01+00:00",
            "latency_ms": 6.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.bank_of_botswana.fetch_text", return_value=text_result("<html>replacement</html>")
        ), mock.patch(
            "adapters.venues.bank_of_botswana.fetch_bytes", return_value=blocked
        ):
            batch = BankOfBotswanaAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["framework"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["mpc_decision"]["fetch_status"])
        self.assertIn("framework parser failed", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual(2, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual("public_botswana_policy_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual("public_botswana_policy_source_unavailable", batch.observations[1]["candidate_reject_reason"])

    def test_plugin_discovery_runtime_and_capability_reconciliation(self) -> None:
        adapter_id = "bank_of_botswana_monetary_policy"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, BankOfBotswanaAdapter)
        self.assertEqual(FRAMEWORK_URL, adapter.info.docs_url)
        self.assertIn("standing_facility_corridor", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_botswana() -> list[str]:
            return [candidate for candidate in original_discover() if candidate == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.bank_of_botswana.fetch_text", return_value=text_result(FRAMEWORK)
        ), mock.patch(
            "adapters.venues.bank_of_botswana.fetch_bytes", return_value=text_result(MPC_DECISION)
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_botswana):
            batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual(5, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #851: Bank of Botswana",
                "market_key": "global_discovery|Bank of Botswana",
                "spec": {"candidate": {"venue_or_source": "Bank of Botswana", "public_docs_url": FRAMEWORK_URL, "data_access_type": "public_no_key"}},
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
