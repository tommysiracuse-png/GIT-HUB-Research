from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import research_worker  # noqa: E402
import storage  # noqa: E402


class ResearchWorkerTests(unittest.TestCase):
    def test_evidence_bundle_preserves_source_and_blocks_runtime_use_until_tests(self) -> None:
        bundle = research_worker.evidence_bundle(
            "https://example.com/docs",
            "Public ticker endpoint exists.",
            "Potential new market venue.",
            "Add parser fixture and pass sandbox tests.",
        )

        self.assertEqual(bundle["source_url"], "https://example.com/docs")
        self.assertEqual(bundle["allowed_use"], "research_only_until_sandbox_and_tests_pass")
        self.assertIn("captured_at", bundle)

    def test_unknown_market_types_are_accepted_and_classified(self) -> None:
        candidate = research_worker.normalize_market_candidate(
            {
                "surface_type_raw": "weather signal feed",
                "venue_or_source": "Example Weather Exchange",
                "asset_or_event": "rainfall contracts",
                "public_docs_url": "https://example.com/weather",
                "source_urls": ["https://example.com/weather"],
                "priority": 71,
            }
        )

        self.assertEqual(candidate["surface_type_classified"], "unknown_global_surface")
        self.assertEqual(candidate["recommended_next_action"], "growth_experiment")
        self.assertIn("candidate_id", candidate)

    def test_run_once_writes_discovery_report_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_json = research_worker.REPORT_JSON
            old_md = research_worker.REPORT_MD
            old_candidates = research_worker.CANDIDATES_JSONL
            old_runs = research_worker.RUNS_DIR
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            import storage

            storage.init_db(conn)
            try:
                research_worker.RUNS_DIR = pathlib.Path(tmp)
                research_worker.REPORT_JSON = pathlib.Path(tmp) / "research_worker_latest.json"
                research_worker.REPORT_MD = pathlib.Path(tmp) / "research_worker_report.md"
                research_worker.CANDIDATES_JSONL = pathlib.Path(tmp) / "market_discovery_candidates.jsonl"

                report = research_worker.run_once(
                    settings={
                        "research_worker": {
                            "enabled": True,
                            "global_market_discovery": True,
                            "max_candidates_per_run": 3,
                            "max_artifacts_per_run": 3,
                            "artifact_priority_floor": 70,
                        }
                    },
                    conn=conn,
                )

                self.assertEqual(report["status"], "ok")
                self.assertGreater(report["summary"]["candidate_count"], 0)
                self.assertGreater(report["summary"]["new_candidate_count"], 0)
                self.assertTrue(report["created_artifacts"])
                current_open_routes = conn.execute(
                    "select count(*) from route_probe_tasks where status='open' and market_key like 'global_discovery|%'"
                ).fetchone()[0]
                self.assertEqual(report["route_lifecycle"]["open_global_route_probes"], current_open_routes)
                self.assertTrue(research_worker.REPORT_JSON.exists())
                self.assertTrue(research_worker.CANDIDATES_JSONL.exists())
                saved = json.loads(research_worker.REPORT_JSON.read_text(encoding="utf-8"))
                self.assertEqual(saved["hard_rule"], report["hard_rule"])
                self.assertIn("Global market discovery", research_worker.REPORT_MD.read_text(encoding="utf-8"))
            finally:
                conn.close()
                research_worker.REPORT_JSON = old_json
                research_worker.REPORT_MD = old_md
                research_worker.CANDIDATES_JSONL = old_candidates
                research_worker.RUNS_DIR = old_runs

    def test_discovery_scan_does_not_falsely_complete_adapter_lifecycle(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        conn.execute(
            """
            insert into growth_experiments (created_at, priority, signal_key, hypothesis, action, evidence_json, status)
            values ('now', 82, 'global_discovery|listed_derivatives',
                    'Explore London Stock Exchange UK equities and ADR relationships',
                    'done', '{}', 'implemented_global_market_discovery_scan')
            """
        )
        conn.commit()
        candidate = research_worker.normalize_market_candidate(
            {
                "surface_type_raw": "local exchange equities and derivatives",
                "venue_or_source": "London Stock Exchange",
                "asset_or_event": "UK equities and ADR relationships",
                "public_docs_url": "https://example.com/lse",
                "data_access_type": "public_no_key",
                "recommended_next_action": "adapter_spec",
                "priority": 82,
            }
        )

        created = research_worker.create_downstream_artifacts(
            conn,
            [candidate],
            {"research_worker": {"suppress_implemented_global_discovery_artifacts": True}},
        )

        self.assertEqual(created[0]["type"], "adapter_spec")
        self.assertTrue(created[0]["inserted"])
        open_rows = conn.execute("select * from growth_experiments where status='open'").fetchall()
        self.assertEqual(open_rows, [])
        conn.close()

    def test_new_global_discovery_artifact_still_gets_created(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        conn.execute(
            """
            insert into growth_experiments (created_at, priority, signal_key, hypothesis, action, evidence_json, status)
            values ('now', 82, 'global_discovery|equity_or_proxy',
                    'Explore London Stock Exchange UK equities and ADR relationships',
                    'done', '{}', 'implemented_global_market_discovery_scan')
            """
        )
        conn.commit()
        candidate = research_worker.normalize_market_candidate(
            {
                "surface_type_raw": "agricultural auction market",
                "venue_or_source": "Nairobi Coffee Exchange",
                "asset_or_event": "coffee auction settlement prices",
                "public_docs_url": "https://example.com/nairobi-coffee",
                "recommended_next_action": "growth_experiment",
                "priority": 88,
            }
        )

        created = research_worker.create_downstream_artifacts(
            conn,
            [candidate],
            {"research_worker": {"suppress_implemented_global_discovery_artifacts": True}},
        )

        self.assertEqual(created[0]["type"], "growth_experiment")
        self.assertFalse(created[0].get("skipped", False))
        open_rows = conn.execute("select hypothesis from growth_experiments where status='open'").fetchall()
        self.assertEqual(len(open_rows), 1)
        self.assertIn("Nairobi Coffee Exchange", open_rows[0]["hypothesis"])
        conn.close()

    def test_continuous_discovery_rotates_themes_and_requires_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_runs = research_worker.RUNS_DIR
            research_worker.RUNS_DIR = pathlib.Path(tmp)
            calls = []

            def fake_complete(*args, **kwargs):
                calls.append(kwargs)
                return research_worker.ModelResult(
                    text=json.dumps(
                        {
                            "discoveries": [
                                {
                                    "surface_type_raw": "regulated electricity spot auction",
                                    "venue_or_source": "Example Power Pool",
                                    "country": "Exampleland",
                                    "region": "East Africa",
                                    "asset_or_event": "day-ahead electricity settlement prices",
                                    "data_access_type": "public_no_key",
                                    "tradability_guess": "watch_only",
                                    "source_urls": ["https://power.example.org/market-data"],
                                    "why_interesting": "Public settlement prices expose a new market surface.",
                                    "inefficiency_hypothesis": "Power shocks may lead listed utility proxies.",
                                    "priority": 82,
                                    "confidence": 0.76,
                                },
                                {
                                    "venue_or_source": "Missing Source Venue",
                                    "asset_or_event": "unknown contract",
                                    "priority": 80,
                                },
                            ],
                            "follow_up_queries": ["Find public power derivatives linked to the discovered pool"],
                        }
                    ),
                    model_name="openai/test-search",
                    model_tier="fast",
                    prompt_tokens=100,
                    completion_tokens=100,
                    estimated_cost_usd=0.01,
                    status="model_call:responses",
                )

            try:
                settings = {
                    "research_worker": {
                        "web_research_enabled": True,
                        "search_themes_per_cycle": 2,
                        "search_theme_cooldown_hours": 168,
                        "max_web_discoveries_per_cycle": 5,
                    }
                }
                first = research_worker.run_continuous_web_discovery(settings, [], model_complete=fake_complete)
                second = research_worker.run_continuous_web_discovery(settings, first["candidates"], model_complete=fake_complete)

                self.assertEqual(first["status"], "ok")
                self.assertEqual(len(first["candidates"]), 1)
                self.assertEqual(first["rejected"][0]["reason"], "missing_public_source_url")
                self.assertEqual(calls[0]["tools"], [{"type": "web_search"}])
                self.assertNotEqual(
                    {item["theme_id"] for item in first["selected_themes"]},
                    {item["theme_id"] for item in second["selected_themes"]},
                )
                self.assertTrue((pathlib.Path(tmp) / "research_discovery_frontier.json").exists())
                self.assertTrue((pathlib.Path(tmp) / "research_discovery_journal.jsonl").exists())
            finally:
                research_worker.RUNS_DIR = old_runs

    def test_adapter_and_route_artifacts_have_independent_lifecycles(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        candidate = research_worker.normalize_market_candidate(
            {
                "surface_type_raw": "public commodity exchange",
                "venue_or_source": "Example Commodity Exchange",
                "asset_or_event": "coffee futures",
                "data_access_type": "public_no_key",
                "tradability_guess": "route_needed",
                "source_urls": ["https://commodities.example.org/docs"],
                "route_blockers": ["futures_account"],
                "priority": 90,
            }
        )

        created = research_worker.create_downstream_artifacts(conn, [candidate], {"research_worker": {}})

        self.assertEqual({item["type"] for item in created}, {"adapter_spec", "route_probe_task"})
        conn.execute(
            "update route_probe_tasks set status='implemented_global_market_discovery_scan' where market_key like 'global_discovery|%'"
        )
        conn.commit()
        reconciled = research_worker.reconcile_discovery_route_lifecycle(conn)
        self.assertEqual(reconciled["reopened_legacy_route_probes"], 1)
        self.assertEqual(reconciled["open_global_route_probes"], 1)
        self.assertEqual(research_worker.create_downstream_artifacts(conn, [candidate], {}), [])
        conn.close()

    def test_five_point_model_priority_is_normalized_to_artifact_scale(self) -> None:
        self.assertEqual(research_worker._normalize_priority(5), 90)
        self.assertEqual(research_worker._normalize_priority(4), 82)
        self.assertEqual(research_worker._normalize_priority(3), 74)
        self.assertEqual(research_worker._normalize_priority(75), 75)


if __name__ == "__main__":
    unittest.main()
