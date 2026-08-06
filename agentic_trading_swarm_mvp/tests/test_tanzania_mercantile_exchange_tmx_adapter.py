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
from adapters.venues.tanzania_mercantile_exchange_tmx import (
    INFODESK_URL,
    MARKET_DATA_URL,
    TanzaniaMercantileExchangeTmxAdapter,
    parse_tanzania_mercantile_exchange_market_csv,
)


MARKET_CSV = """Commodity,Code,Location,High Price (TZS/kg),Low Price (TZS/kg),Date,Price Change (TZS/kg),ID,Volume
Sesame Seeds,SS,Tanga,2420,2410,2026-08-05,-40,1708,121815
Chick peas,CP,Manyara,1475,1425,2026-08-05,245,1710,450000
GreenGrams,GG,Dodoma,1270,1270,2026-06-02,0,1579,117600
Coffee-Robusta,CF-RB,Kagera,4710,3810,2026-07-30,860,1684,1786775
Pigeon peas,PP,Arusha,1110,1110,2025-12-30,-10,1473,200000
"""


def text_result(text: str, received_at: str = "2026-08-06T05:15:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class TanzaniaMercantileExchangeTmxAdapterTests(unittest.TestCase):
    def test_parser_normalizes_current_season_pulses_and_oilseeds_rows(self) -> None:
        rows = parse_tanzania_mercantile_exchange_market_csv(
            MARKET_CSV,
            received_at="2026-08-06T05:15:00+00:00",
        )
        self.assertEqual(3, len(rows))
        by_inst = {row["inst_id"]: row for row in rows}
        sesame = next(row for row in by_inst.values() if row["base"] == "SESAME_SEEDS")
        self.assertEqual(2420.0, sesame["last"])
        self.assertEqual(10.0, sesame["published_price_spread_tzs_per_kg"])
        self.assertEqual("fresh", sesame["freshness_state"])
        self.assertEqual("completed", sesame["session_status"])
        self.assertEqual("location_session_row", sesame["source_granularity"])
        self.assertFalse(sesame["lot_level_disclosure_available"])
        self.assertTrue(sesame["paper_experiment_eligible"])

        greengrams = next(row for row in by_inst.values() if row["base"] == "GREEN_GRAMS")
        self.assertEqual("stale", greengrams["freshness_state"])
        self.assertEqual(117600.0, greengrams["published_volume_kg"])
        self.assertEqual("official_auction_session_result", greengrams["quality_status"])

    def test_runtime_discovery_scan_and_failure_evidence(self) -> None:
        adapter_id = "tanzania_mercantile_exchange_tmx_pulses_oilseeds"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, TanzaniaMercantileExchangeTmxAdapter)
        self.assertEqual("TMX", adapter.info.venue)
        self.assertEqual(INFODESK_URL, adapter.info.docs_url)
        self.assertIn("auction_results", adapter.info.capabilities)
        self.assertIn("public_market_data", adapter.info.capabilities)

        with mock.patch(
            "adapters.venues.tanzania_mercantile_exchange_tmx.fetch_text",
            return_value=text_result(MARKET_CSV),
        ):
            batch = TanzaniaMercantileExchangeTmxAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["market_data_csv"]["fetch_status"])
        self.assertEqual(3, batch.metadata["real_observation_count"])
        self.assertEqual(3, batch.metadata["commodity_count"])
        self.assertEqual("mixed", batch.metadata["freshness_state"])
        self.assertEqual("recent_completed_sessions", batch.metadata["session_state"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(all(row["last"] > 0 for row in batch.observations))

        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.tanzania_mercantile_exchange_tmx.fetch_text",
            return_value=text_result(MARKET_CSV),
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime,
            "discover_adapters",
            side_effect=lambda: [item for item in original_discover() if item == adapter_id],
        ):
            runtime_batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )
        report = runtime_batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(3, report["adapters"][0]["price_observation_count"])

        with mock.patch(
            "adapters.venues.tanzania_mercantile_exchange_tmx.fetch_text",
            return_value={
                "ok": False,
                "status": "blocked",
                "http_status": 403,
                "text": "",
                "received_at": "2026-08-06T05:15:00+00:00",
                "latency_ms": 5.0,
                "error": "HTTP Error 403",
            },
        ):
            failed = TanzaniaMercantileExchangeTmxAdapter().scan({})
        self.assertEqual("blocked", failed.metadata["source_status"])
        self.assertEqual(0, failed.metadata["real_observation_count"])
        self.assertEqual(1, len(failed.observations))
        self.assertEqual("public_tmx_source_unavailable", failed.observations[0]["candidate_reject_reason"])
        self.assertEqual("watch_only", failed.observations[0]["direction"])

        with mock.patch(
            "adapters.venues.tanzania_mercantile_exchange_tmx.fetch_text",
            return_value=text_result("<html>replacement</html>"),
        ):
            degraded = TanzaniaMercantileExchangeTmxAdapter().scan({})
        self.assertEqual("degraded", degraded.metadata["source_status"])
        self.assertEqual(1, len(degraded.metadata["parser_failures"]))
        self.assertEqual("public_tmx_parser_failure", degraded.observations[0]["candidate_reject_reason"])

    def test_capability_reconciliation_matches_spec_1303(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #1303: Tanzania Mercantile Exchange (TMX)",
                "market_key": "global_discovery|Tanzania Mercantile Exchange (TMX)",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Tanzania Mercantile Exchange (TMX)",
                        "public_docs_url": INFODESK_URL,
                        "source_urls": [MARKET_DATA_URL],
                        "asset_or_event": "pulses and oilseeds live auction lots season 2026/27",
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("tanzania_mercantile_exchange_tmx_pulses_oilseeds", match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
