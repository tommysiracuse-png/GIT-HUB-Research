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
import adapter_capabilities
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.sadc_south_african_reserve_bank import (
    ANNOUNCEMENT_URL,
    SARB_RTGS_URL,
    SadcSouthAfricanReserveBankAdapter,
    parse_kwanza_onboarding,
    parse_participant_roster,
)


ANNOUNCEMENT = """
<html><body>
  <p>July 28, 2026</p>
  <h1>Angolan Kwanza introduced into the SADC-RTGS system</h1>
  <p>Lesetja Kganyago, Governor of the South African Reserve Bank, and Manuel
  Tiago Dias, Governor of the Banco Nacional de Angola, on 27 July 2026 formally
  announced the introduction of the Angolan kwanza as a settlement currency in
  the SADC real-time gross settlement (SADC-RTGS) system.</p>
  <p>The Angolan kwanza is the second settlement currency to be introduced in
  the SADC-RTGS system, which has settled transactions exclusively in South
  African rand since its inception in 2013. There are currently 15 countries
  participating in the SADC-RTGS system.</p>
</body></html>
"""

ROSTER = """
<html><body>
  <h1>Regional Settlement Services</h1>
  <p>The SADC-RTGS is an automated interbank settlement system operated by the
  South African Reserve Bank, as appointed by the SADC participating member
  central banks.</p>
  <p>Membership comprises 16 countries namely, Angola, Botswana, Comoros,
  Democratic Republic of Congo, Eswatini, Lesotho, Madagascar, Malawi,
  Mauritius, Mozambique, Namibia, Seychelles, South Africa, Tanzania, Zambia
  and Zimbabwe.</p>
  <p>In 1992, Member States signed the SADC Treaty.</p>
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


class SadcSouthAfricanReserveBankAdapterTests(unittest.TestCase):
    def test_parsers_normalize_aoa_onboarding_and_16_country_roster(self) -> None:
        onboarding = parse_kwanza_onboarding(
            ANNOUNCEMENT, received_at="2026-08-04T08:30:00+00:00"
        )[0]
        roster = parse_participant_roster(ROSTER, received_at="2026-08-04T08:30:00+00:00")[0]

        self.assertEqual("SADC_RTGS:SETTLEMENT_CURRENCY:AOA", onboarding["inst_id"])
        self.assertEqual("AOA", onboarding["settlement_currency"])
        self.assertEqual("ZAR", onboarding["prior_settlement_currency"])
        self.assertEqual(2, onboarding["settlement_currency_count"])
        self.assertEqual("2026-07-27", onboarding["onboarding_date"])
        self.assertEqual("fresh", onboarding["freshness_state"])
        self.assertEqual("multicurrency_settlement_enabled", onboarding["session_status"])
        self.assertEqual(ANNOUNCEMENT_URL, onboarding["source_url"])
        self.assertEqual("watch_only", onboarding["direction"])

        self.assertEqual("SADC_RTGS:PARTICIPANT_ROSTER", roster["inst_id"])
        self.assertEqual(16, roster["member_country_count"])
        self.assertEqual(16, roster["participant_country_count"])
        self.assertEqual("Angola", roster["member_countries"][0])
        self.assertEqual("Zimbabwe", roster["member_countries"][-1])
        self.assertEqual("regional_settlement_system_reference", roster["session_status"])
        self.assertEqual(SARB_RTGS_URL, roster["source_url"])
        self.assertEqual("watch_only", roster["direction"])

    def test_scan_retains_per_source_fetch_and_parser_failure_evidence(self) -> None:
        malformed = fetch_result("<html><body>replacement page</body></html>")
        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T08:30:01+00:00",
            "latency_ms": 6.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.sadc_south_african_reserve_bank.fetch_text",
            side_effect=[malformed, unavailable],
        ):
            batch = SadcSouthAfricanReserveBankAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["announcement"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["roster"]["fetch_status"])
        self.assertIn("announcement parser failed", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(2, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual(
            "public_settlement_reference_parser_failure",
            batch.observations[0]["candidate_reject_reason"],
        )
        self.assertEqual(
            "public_settlement_reference_source_unavailable",
            batch.observations[1]["candidate_reject_reason"],
        )
        self.assertEqual("unknown", batch.observations[0]["freshness_state"])
        self.assertEqual("unknown", batch.observations[1]["session_status"])

    def test_plugin_is_runtime_discoverable_and_returns_real_reference_observations(self) -> None:
        adapter_id = "sadc_south_african_reserve_bank"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, SadcSouthAfricanReserveBankAdapter)
        self.assertEqual(ANNOUNCEMENT_URL, adapter.info.docs_url)
        self.assertIn("settlement_currency_onboarding", adapter.info.capabilities)
        self.assertIn("participant_roster", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_sadc() -> list[str]:
            return [adapter_id for adapter_id in original_discover() if adapter_id == "sadc_south_african_reserve_bank"]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.sadc_south_african_reserve_bank.fetch_text",
            side_effect=[fetch_result(ANNOUNCEMENT), fetch_result(ROSTER)],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_sadc):
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

    def test_spec_462_is_covered_by_the_runtime_adapter(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #462: SADC / South African Reserve Bank",
                "market_key": "global_discovery|SADC / South African Reserve Bank",
                "spec": {
                    "candidate": {
                        "venue_or_source": "SADC / South African Reserve Bank",
                        "public_docs_url": ANNOUNCEMENT_URL,
                        "asset_or_event": (
                            "SADC-RTGS settlement currency onboarding: Angolan kwanza (AOA)"
                        ),
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )

        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("sadc_south_african_reserve_bank", match["adapter_id"])
        self.assertIn("settlement_reference", match["available_capabilities"])


if __name__ == "__main__":
    unittest.main()
