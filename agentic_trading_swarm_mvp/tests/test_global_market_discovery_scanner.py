from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import global_market_discovery_scanner as scanner  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["global_market_discovery_scanner"] = {
        "enabled": True,
        "min_discovery_priority": 70,
        "max_surfaces_per_cycle": 6,
        "max_proxy_symbols_per_surface": 2,
        "workers": 1,
        "merge_default_seeds": False,
        "watch_only_stale_minutes": 180,
    }
    cfg["scanner"]["paper_trade_conditional"] = True
    return cfg


def fake_chart(symbol: str, *args: object, **kwargs: object) -> dict:
    del args, kwargs
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    closes = [100.0 + idx * 0.2 for idx in range(30)]
    if symbol.endswith("SHORT"):
        closes = [110.0 - idx * 0.2 for idx in range(30)]
    return {
        "meta": {"symbol": symbol, "exchangeName": "TEST", "currency": "USD"},
        "timestamp": [now - (29 - idx) * 900 for idx in range(30)],
        "indicators": {"quote": [{"close": closes, "volume": [1000000] * 30}]},
    }


class GlobalMarketDiscoveryScannerTests(unittest.TestCase):
    def test_latest_report_does_not_hide_durable_discovery_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_report = scanner.RESEARCH_REPORT_JSON
            old_ledger = scanner.DISCOVERY_JSONL
            try:
                scanner.RESEARCH_REPORT_JSON = pathlib.Path(tmp) / "research_worker_latest.json"
                scanner.DISCOVERY_JSONL = pathlib.Path(tmp) / "market_discovery_candidates.jsonl"
                recent = scanner.normalize_market_candidate(
                    {
                        "surface_type_raw": "new exchange",
                        "venue_or_source": "Recent Exchange",
                        "asset_or_event": "recent asset",
                        "priority": 90,
                        "discovered_by": "openai_responses_web_search",
                    }
                )
                older = scanner.normalize_market_candidate(
                    {
                        "surface_type_raw": "older exchange",
                        "venue_or_source": "Older Exchange",
                        "asset_or_event": "older asset",
                        "priority": 88,
                    }
                )
                scanner.RESEARCH_REPORT_JSON.write_text(json.dumps({"candidates": [recent]}), encoding="utf-8")
                scanner.DISCOVERY_JSONL.write_text(json.dumps(older) + "\n", encoding="utf-8")

                loaded = scanner.load_discovery_candidates(
                    {
                        "global_market_discovery_scanner": {
                            "merge_default_seeds": False,
                            "min_discovery_priority": 1,
                            "max_surfaces_per_cycle": 10,
                            "recent_discovery_rotation_slots": 2,
                        }
                    }
                )

                self.assertEqual({item["venue_or_source"] for item in loaded}, {"Recent Exchange", "Older Exchange"})
            finally:
                scanner.RESEARCH_REPORT_JSON = old_report
                scanner.DISCOVERY_JSONL = old_ledger

    def test_default_proxy_map_covers_broader_global_surfaces(self) -> None:
        expected = {
            "London Stock Exchange",
            "TMX Group",
            "Hong Kong Exchanges and Clearing",
            "Euronext",
            "Taiwan Stock Exchange",
            "Korea Exchange",
            "Australian Securities Exchange",
            "Johannesburg Stock Exchange",
            "Saudi Exchange",
            "Cboe Global Markets",
            "Intercontinental Exchange",
            "London Metal Exchange",
        }

        self.assertTrue(expected.issubset(set(scanner.DEFAULT_PROXY_MAP)))
        self.assertEqual(scanner.DEFAULT_PROXY_MAP["London Stock Exchange"][0]["symbol"], "EWU")
        self.assertEqual(scanner.DEFAULT_PROXY_MAP["Cboe Global Markets"][0]["symbol"], "VIXY")

    def test_research_report_merges_default_seeds_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_report = scanner.RESEARCH_REPORT_JSON
            old_jsonl = scanner.DISCOVERY_JSONL
            tmp_path = pathlib.Path(tmp)
            try:
                scanner.RESEARCH_REPORT_JSON = tmp_path / "research_worker_latest.json"
                scanner.DISCOVERY_JSONL = tmp_path / "market_discovery_candidates.jsonl"
                scanner.RESEARCH_REPORT_JSON.write_text(
                    json.dumps(
                        {
                            "candidates": [
                                {
                                    "candidate_id": "only_existing_b3",
                                    "surface_type_raw": "equity and ETF local exchange market data",
                                    "surface_type_classified": "equity_or_proxy",
                                    "venue_or_source": "B3",
                                    "priority": 82,
                                    "confidence": 0.68,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                cfg = settings()
                cfg["global_market_discovery_scanner"]["merge_default_seeds"] = True
                cfg["global_market_discovery_scanner"]["max_surfaces_per_cycle"] = 80

                candidates = scanner.load_discovery_candidates(cfg)

                venues = {row.get("venue_or_source") for row in candidates}
                self.assertIn("B3", venues)
                self.assertIn("London Stock Exchange", venues)
                self.assertIn("Cboe Global Markets", venues)
            finally:
                scanner.RESEARCH_REPORT_JSON = old_report
                scanner.DISCOVERY_JSONL = old_jsonl

    def test_research_worker_surface_becomes_normal_priceable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_report = scanner.RESEARCH_REPORT_JSON
            old_jsonl = scanner.DISCOVERY_JSONL
            old_report_json = scanner.REPORT_JSON
            old_report_md = scanner.REPORT_MD
            old_fetch = scanner.fetch_chart
            tmp_path = pathlib.Path(tmp)
            try:
                scanner.RESEARCH_REPORT_JSON = tmp_path / "research_worker_latest.json"
                scanner.DISCOVERY_JSONL = tmp_path / "market_discovery_candidates.jsonl"
                scanner.REPORT_JSON = tmp_path / "global_market_discovery_scan_latest.json"
                scanner.REPORT_MD = tmp_path / "global_market_discovery_scan_report.md"
                scanner.fetch_chart = fake_chart
                scanner.RESEARCH_REPORT_JSON.write_text(
                    json.dumps(
                        {
                            "candidates": [
                                {
                                    "candidate_id": "gmd_b3",
                                    "surface_type_raw": "equity and ETF local exchange market data",
                                    "surface_type_classified": "equity_or_proxy",
                                    "venue_or_source": "B3",
                                    "country": "Brazil",
                                    "region": "LATAM",
                                    "asset_or_event": "Brazil equities and ETFs",
                                    "data_access_type": "public_no_key",
                                    "tradability_guess": "route_needed",
                                    "public_docs_url": "https://example.com/b3",
                                    "source_urls": ["https://example.com/b3"],
                                    "why_interesting": "Brazil local market proxy dislocations.",
                                    "inefficiency_hypothesis": "ADR and ETF proxies may lag local markets.",
                                    "latency_sensitivity": "medium",
                                    "liquidity_hint": "large local exchange",
                                    "route_blockers": ["broker_route"],
                                    "recommended_next_action": "growth_experiment",
                                    "priority": 82,
                                    "confidence": 0.68,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                batch = scanner.build_scan_batch(settings(), limit=5)

                self.assertEqual(batch.source, "global_market_discovery_scanner")
                self.assertTrue(batch.candidates)
                first = batch.candidates[0]
                self.assertEqual(first["venue"], "B3")
                self.assertEqual(first["trade_type"], "global_market_discovery_proxy")
                self.assertEqual(first["direction"], "long_proxy")
                self.assertIn("last", first)
                self.assertTrue(any(row["inst_id"] == first["inst_id"] for row in batch.observations))
                self.assertTrue(scanner.REPORT_JSON.exists())
                report = json.loads(scanner.REPORT_JSON.read_text(encoding="utf-8"))
                self.assertGreater(report["summary"]["priceable_candidates"], 0)
            finally:
                scanner.RESEARCH_REPORT_JSON = old_report
                scanner.DISCOVERY_JSONL = old_jsonl
                scanner.REPORT_JSON = old_report_json
                scanner.REPORT_MD = old_report_md
                scanner.fetch_chart = old_fetch

    def test_unmapped_discovery_stays_watch_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_report = scanner.RESEARCH_REPORT_JSON
            old_jsonl = scanner.DISCOVERY_JSONL
            old_report_json = scanner.REPORT_JSON
            old_report_md = scanner.REPORT_MD
            tmp_path = pathlib.Path(tmp)
            try:
                scanner.RESEARCH_REPORT_JSON = tmp_path / "research_worker_latest.json"
                scanner.DISCOVERY_JSONL = tmp_path / "market_discovery_candidates.jsonl"
                scanner.REPORT_JSON = tmp_path / "global_market_discovery_scan_latest.json"
                scanner.REPORT_MD = tmp_path / "global_market_discovery_scan_report.md"
                scanner.RESEARCH_REPORT_JSON.write_text(
                    json.dumps(
                        {
                            "candidates": [
                                {
                                    "candidate_id": "gmd_weather",
                                    "surface_type_raw": "weather signal feed",
                                    "surface_type_classified": "unknown_global_surface",
                                    "venue_or_source": "Example Weather Exchange",
                                    "country": "Global",
                                    "region": "Global",
                                    "asset_or_event": "rainfall contracts",
                                    "data_access_type": "public_no_key",
                                    "tradability_guess": "unknown",
                                    "public_docs_url": "https://example.com/weather",
                                    "source_urls": ["https://example.com/weather"],
                                    "why_interesting": "Unmapped alternative market.",
                                    "inefficiency_hypothesis": "Unknown.",
                                    "latency_sensitivity": "low",
                                    "liquidity_hint": "unknown",
                                    "route_blockers": [],
                                    "recommended_next_action": "growth_experiment",
                                    "priority": 75,
                                    "confidence": 0.5,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                batch = scanner.build_scan_batch(settings(), limit=5)

                self.assertEqual(len(batch.candidates), 1)
                self.assertEqual(batch.candidates[0]["direction"], "watch_only")
                self.assertEqual(batch.observations, [])
                self.assertEqual(batch.metadata["global_market_discovery_scan"]["watch_only_candidates"], 1)
            finally:
                scanner.RESEARCH_REPORT_JSON = old_report
                scanner.DISCOVERY_JSONL = old_jsonl
                scanner.REPORT_JSON = old_report_json
                scanner.REPORT_MD = old_report_md

    def test_required_open_instrument_is_repriced_even_when_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_report = scanner.RESEARCH_REPORT_JSON
            old_jsonl = scanner.DISCOVERY_JSONL
            old_report_json = scanner.REPORT_JSON
            old_report_md = scanner.REPORT_MD
            old_fetch = scanner.fetch_chart
            tmp_path = pathlib.Path(tmp)
            try:
                scanner.RESEARCH_REPORT_JSON = tmp_path / "research_worker_latest.json"
                scanner.DISCOVERY_JSONL = tmp_path / "market_discovery_candidates.jsonl"
                scanner.REPORT_JSON = tmp_path / "global_market_discovery_scan_latest.json"
                scanner.REPORT_MD = tmp_path / "global_market_discovery_scan_report.md"
                scanner.fetch_chart = fake_chart
                scanner.RESEARCH_REPORT_JSON.write_text(json.dumps({"candidates": []}), encoding="utf-8")

                batch = scanner.build_scan_batch(settings(), limit=5, required_inst_ids={"CUSTOM_MARKET:EWZ"})

                self.assertTrue(any(row["inst_id"] == "CUSTOM_MARKET:EWZ" for row in batch.observations))
                required = next(row for row in batch.candidates if row["inst_id"] == "CUSTOM_MARKET:EWZ")
                self.assertEqual(required["trade_type"], "global_market_discovery_proxy")
            finally:
                scanner.RESEARCH_REPORT_JSON = old_report
                scanner.DISCOVERY_JSONL = old_jsonl
                scanner.REPORT_JSON = old_report_json
                scanner.REPORT_MD = old_report_md
                scanner.fetch_chart = old_fetch


if __name__ == "__main__":
    unittest.main()
