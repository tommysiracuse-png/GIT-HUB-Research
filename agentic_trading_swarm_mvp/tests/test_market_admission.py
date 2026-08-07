import copy
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

import market_admission
from settings import DEFAULT_SETTINGS
from storage import init_db, open_paper_trade


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings():
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["market_admission"] = {
        "enabled": True,
        "consecutive_failures_degraded": 2,
        "diagnostic_after_eligible_scans": 2,
        "implementation_task_after_eligible_scans": 3,
        "requested_symbols": ["EWY"],
    }
    return cfg


def global_candidate(**overrides):
    item = {
        "venue": "KOREA_EXCHANGE",
        "inst_id": "KOREA_EXCHANGE:EWY",
        "proxy_symbol": "EWY",
        "proxy_surface": "country_etf",
        "market_surface": "global_market_discovery",
        "trade_type": "global_market_discovery_proxy",
        "direction": "long_proxy",
        "last": 74.0,
        "score": 60.0,
        "edge_bps_estimate": 12.0,
        "liquidity_score": 0.8,
        "spread_bps": 2.0,
        "stale_minutes": 2.0,
        "session_status": "open",
        "proxy_quality_status": "verified_proxy",
        "data_status": "reachable",
        "signal_lineage_key": "GLOBAL_ACTIVE|country_etf|country_adr_relative_momentum_v1",
        "execution_feasibility": {"status": "standard"},
        "data_source": {"provider": "Yahoo market data"},
    }
    item.update(overrides)
    return item


class MarketAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_json = market_admission.REPORT_JSON
        self.old_md = market_admission.REPORT_MD
        market_admission.REPORT_JSON = pathlib.Path(self.temp.name) / "admission.json"
        market_admission.REPORT_MD = pathlib.Path(self.temp.name) / "admission.md"

    def tearDown(self):
        market_admission.REPORT_JSON = self.old_json
        market_admission.REPORT_MD = self.old_md
        self.temp.cleanup()

    def test_tracks_paper_eligible_global_strategy_separately(self):
        with memory_db() as conn:
            candidate = global_candidate()
            review = {"decision": "approve_paper_trade", "hard_blocks": []}
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": review}],
            )
            state = report["states"][0]
            self.assertEqual("paper_eligible", state["current_stage"])
            self.assertEqual("healthy", state["health_status"])
            self.assertIn("country_adr_relative_momentum_v1", state["strategy_lineage"])

    def test_cross_venue_and_surface_reviews_keep_canonical_identities_independent(self):
        shared = {
            "inst_id": "SHARED-USDT-SWAP",
            "signal_lineage_key": "SHARED|perp_basis|v1",
            "direction": "short_perp_long_spot",
            "trade_type": "perp_funding_basis",
            "proxy_quality_status": "verified_proxy",
        }
        candidates = [
            global_candidate(
                **shared,
                venue="VENUE_A",
                proxy_surface="perpetual",
                market_surface="perpetual",
            ),
            global_candidate(
                **shared,
                venue="VENUE_B",
                proxy_surface="perpetual",
                market_surface="perpetual",
            ),
            global_candidate(
                **shared,
                venue="VENUE_A",
                proxy_surface="spot_basis",
                market_surface="spot_basis",
            ),
        ]
        decisions = (
            "approve_paper_trade",
            "reject",
            "approve_conditional_paper_trade",
        )
        reviewed = []
        expected = {}
        for index, (candidate, decision) in enumerate(zip(candidates, decisions), start=1):
            admission_key = market_admission.admission_key_for(candidate)
            episode_id = f"episode-{index}"
            reviewed_candidate = {
                **candidate,
                "admission_key": admission_key,
                "admission_episode_id": episode_id,
                "episode_id": episode_id,
            }
            reviewed.append(
                {
                    "candidate": reviewed_candidate,
                    "review": {"decision": decision, "hard_blocks": []},
                    "opportunity_id": 100 + index,
                }
            )
            expected[admission_key] = (decision, episode_id)

        self.assertEqual(3, len(expected))
        cfg = settings()
        cfg["market_admission"]["requested_symbols"] = ["SHARED-USDT-SWAP"]
        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn,
                cfg,
                candidates,
                reviewed,
                [],
            )
            persisted = conn.execute(
                "select admission_key,venue,market_surface,current_episode_id "
                "from market_admission_states order by admission_key"
            ).fetchall()

        self.assertEqual(3, report["summary"]["touched_state_count"])
        self.assertEqual(3, len(report["states"]))
        self.assertEqual(3, len(persisted))
        states = {state["admission_key"]: state for state in report["states"]}
        self.assertEqual(set(expected), set(states))
        for admission_key, (decision, episode_id) in expected.items():
            with self.subTest(admission_key=admission_key):
                state = states[admission_key]
                self.assertEqual(decision, state["details"]["review_decision"])
                self.assertEqual(episode_id, state["current_episode_id"])
                self.assertEqual(
                    "route_feasible" if decision == "reject" else "paper_eligible",
                    state["current_stage"],
                )
        artifact = json.loads(market_admission.REPORT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(3, len(artifact["states"]))
        self.assertEqual(set(expected), {item["admission_key"] for item in artifact["states"]})
        markdown = market_admission.REPORT_MD.read_text(encoding="utf-8")
        for admission_key in expected:
            self.assertIn(admission_key, markdown)

    def test_proxy_short_missing_quality_evidence_fails_closed_with_report_reason(self):
        candidate = global_candidate(
            direction="short_proxy",
            proxy_quality_status="verified_proxy",
        )
        candidate.pop("stale_minutes")
        candidate.pop("liquidity_score")

        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": {"decision": "approve_paper_trade", "hard_blocks": []}}],
            )

        state = report["states"][0]
        self.assertEqual("priceable", state["current_stage"])
        self.assertEqual("proxy_short_quality_missing_freshness", state["blocker_code"])
        self.assertEqual(
            "proxy_short_quality_missing_freshness",
            state["details"]["quality_failure_reason"],
        )
        self.assertEqual(
            1,
            report["summary"]["by_quality_failure"]["proxy_short_quality_missing_freshness"],
        )

    def test_proxy_short_requires_fresh_depth_and_venue_health_evidence(self):
        candidate = global_candidate(
            direction="short_proxy",
            proxy_quality_status="verified_proxy",
            freshness_age_seconds=30.0,
            proxy_depth_notional_usd=2_000_000.0,
            proxy_depth_basis="recent_traded_notional",
            proxy_venue_health_status="healthy",
            proxy_venue_health_basis="successful_public_chart_parse",
        )

        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": {"decision": "approve_paper_trade", "hard_blocks": []}}],
            )

        state = report["states"][0]
        self.assertEqual("paper_eligible", state["current_stage"])
        self.assertIsNone(state["details"]["quality_failure_reason"])
        self.assertTrue(state["details"]["proxy_short_quality_review"]["eligible"])

    def test_proxy_short_stale_enrichment_is_blocked_before_paper_review(self):
        candidate = global_candidate(
            direction="short_proxy",
            proxy_quality_status="verified_proxy",
            freshness_age_seconds=3601.0,
            proxy_depth_notional_usd=2_000_000.0,
            proxy_depth_basis="recent_traded_notional",
            proxy_venue_health_status="healthy",
        )

        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": {"decision": "approve_paper_trade", "hard_blocks": []}}],
            )

        state = report["states"][0]
        self.assertEqual("priceable", state["current_stage"])
        self.assertEqual("proxy_short_quality_stale", state["blocker_code"])

    def test_bybit_403_is_network_state_not_strategy_failure(self):
        observation = {
            "venue": "BYBIT_SPOT",
            "instrument_id": "BYBIT_SPOT:ALL",
            "market_type": "spot",
            "data_status": "blocked",
            "http_status": "HTTP 403: Forbidden",
            "access_blocker_code": "network_region_blocked",
            "session_status": "continuous",
            "source_url": "https://api.bybit.com/v5/market/tickers?category=spot",
        }
        with memory_db() as conn:
            report = None
            for scan in range(2):
                fresh_observation = {**observation, "source_timestamp": f"2026-08-07T12:00:0{scan}Z"}
                report = market_admission.run_market_admission_monitor(
                    conn, settings(), [], [], [fresh_observation]
                )
            state = report["states"][0]
            self.assertEqual("network_region_blocked", state["blocker_code"])
            self.assertEqual("dormant_until_config_change", state["health_status"])
            self.assertEqual(0, state["attempts"])
            self.assertEqual(0, state["stalled_eligible_scans"])
            self.assertEqual(0, state["consecutive_failures"])
            self.assertEqual("adapter_observation", state["strategy_lineage"])

    def test_b3_cbio_companion_observation_advances_to_quality_verified(self):
        observation = {
            "venue": "B3",
            "inst_id": "B3:PUBLIC_DATA_SURFACE:CBIO",
            "trade_type": "official_market_catalog",
            "market_type": "otc_environmental_reference",
            "market_surface": "b3_cbio_public_data",
            "asset_class": "decarbonization_credit",
            "base": "CBIO",
            "quote": "USD",
            "last": 31.42,
            "price_available": True,
            "price_basis": "public_companion_global_carbon_etf_quote",
            "quality_status": "verified_proxy",
            "proxy_quality_status": "verified_proxy",
            "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
            "direction": "watch_only",
            "freshness_state": "fresh",
            "session_status": "unknown",
            "data_status": "reachable",
            "price_source": "TradingView public carbon ETF companion quote",
            "source_url": "https://www.tradingview.com/symbols/NYSEARCA-KRBN/",
            "source_contract_url": "https://www.b3.com.br/en_us/b3/esg/otc-market.htm",
            "companion_quote_symbol": "KRBN",
            "proxy_symbol": "NYSEARCA:KRBN",
        }

        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [],
                [],
                [observation],
            )

        state = report["states"][0]
        self.assertEqual("quality_verified", state["current_stage"])
        self.assertEqual("public_companion_price_requires_strategy_logic", state["blocker_code"])
        self.assertEqual("adapter_observation", state["strategy_lineage"])

    def test_stall_creates_one_task_and_progress_resolves_it(self):
        stalled = global_candidate(direction="watch_only", candidate_reject_reason="surface_confirmation_missing")
        with memory_db() as conn:
            for scan in range(4):
                fresh_stalled = {**stalled, "source_timestamp": f"2026-08-07T12:00:0{scan}Z"}
                market_admission.run_market_admission_monitor(
                    conn, settings(), [fresh_stalled], [], []
                )
            tasks = conn.execute(
                "select id, status from improvement_tasks where title like 'Market admission cohort stalled [%]'"
            ).fetchall()
            directives = conn.execute(
                "select id, status from market_hunter_directives "
                "where market_key like 'market_admission_cohort|%'"
            ).fetchall()
            self.assertEqual(1, len(tasks))
            self.assertEqual("open", tasks[0]["status"])
            self.assertEqual(1, len(directives))
            self.assertEqual("open", directives[0]["status"])

            healthy = global_candidate()
            review = {"decision": "approve_paper_trade", "hard_blocks": []}
            market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [healthy],
                [{"candidate": healthy, "review": review}],
                [],
            )
            status = conn.execute("select status from improvement_tasks where id = ?", (tasks[0]["id"],)).fetchone()["status"]
            directive_status = conn.execute(
                "select status from market_hunter_directives where id = ?",
                (directives[0]["id"],),
            ).fetchone()["status"]
            self.assertEqual("resolved_market_admission_advanced", status)
            self.assertEqual("resolved_market_admission_advanced", directive_status)

    def test_report_is_machine_readable(self):
        with memory_db() as conn:
            market_admission.run_market_admission_monitor(conn, settings(), [global_candidate()], [], [])
        payload = json.loads(market_admission.REPORT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["summary"]["requested_symbols_observed"])

    def test_legacy_per_instrument_stall_tasks_are_superseded_by_cohort_model(self):
        with memory_db() as conn:
            for idx in range(3):
                conn.execute(
                    """
                    insert into improvement_tasks (created_at, priority, title, rationale, status)
                    values ('now', 95, ?, 'OKX normalization_missing', 'open')
                    """,
                    (f"Market admission stalled [legacy-{idx}]",),
                )
            conn.commit()
            report = market_admission.run_market_admission_monitor(conn, settings(), [], [], [])
            statuses = [row["status"] for row in conn.execute("select status from improvement_tasks")]
        self.assertEqual(["superseded_by_market_admission_cohort"] * 3, statuses)
        self.assertEqual(3, report["summary"]["task_cohorts"]["legacy_instrument_tasks_superseded"])

    def test_duplicate_evidence_does_not_increment_attempt_or_failure_counters(self):
        observation = global_candidate(
            direction="watch_only",
            candidate_reject_reason="surface_confirmation_missing",
            source_timestamp="2026-08-07T12:00:00Z",
        )
        with memory_db() as conn:
            market_admission.run_market_admission_monitor(conn, settings(), [observation], [], [])
            market_admission.run_market_admission_monitor(conn, settings(), [observation], [], [])
            row = conn.execute(
                """
                select attempts,eligible_scans,fresh_evidence_scans,
                       stalled_eligible_scans,consecutive_failures
                from market_admission_states
                """
            ).fetchone()
        self.assertEqual(1, row["attempts"])
        self.assertEqual(1, row["eligible_scans"])
        self.assertEqual(1, row["fresh_evidence_scans"])
        self.assertEqual(0, row["stalled_eligible_scans"])
        self.assertEqual(1, row["consecutive_failures"])

    def test_cached_derived_age_changes_do_not_create_fresh_evidence(self):
        candidate = global_candidate(
            cache_status="cached",
            source_timestamp="2026-08-07T12:00:00Z",
            freshness_age_seconds=30.0,
            signal_age_seconds=30.0,
            stale_minutes=0.5,
        )
        review = {"decision": "approve_paper_trade", "hard_blocks": []}
        older_cache = {
            **candidate,
            "freshness_age_seconds": 60.0,
            "signal_age_seconds": 60.0,
            "stale_minutes": 1.0,
        }
        self.assertEqual(
            market_admission.admission_evidence_fingerprint(candidate),
            market_admission.admission_evidence_fingerprint(older_cache),
        )

        with memory_db() as conn:
            market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": review}],
                [],
            )
            before = conn.execute(
                """
                select attempts,eligible_scans,fresh_evidence_scans,
                       stalled_eligible_scans,consecutive_failures,
                       last_evidence_fingerprint
                from market_admission_states
                """
            ).fetchone()
            transition_count = conn.execute(
                "select count(*) from market_admission_transitions"
            ).fetchone()[0]
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [older_cache],
                [{"candidate": older_cache, "review": review}],
                [],
            )
            after = conn.execute(
                """
                select attempts,eligible_scans,fresh_evidence_scans,
                       stalled_eligible_scans,consecutive_failures,
                       last_evidence_fingerprint
                from market_admission_states
                """
            ).fetchone()
            after_transition_count = conn.execute(
                "select count(*) from market_admission_transitions"
            ).fetchone()[0]

        self.assertEqual(dict(before), dict(after))
        self.assertEqual(transition_count, after_transition_count)
        self.assertFalse(report["states"][0]["details"]["fresh_evidence"])

    def test_monitor_only_persists_without_creating_actions(self):
        cfg = settings()
        cfg["market_admission"]["diagnostics_enabled"] = False
        stalled = global_candidate(
            direction="watch_only",
            candidate_reject_reason="surface_confirmation_missing",
        )
        with memory_db() as conn:
            for scan in range(4):
                market_admission.run_market_admission_monitor(
                    conn,
                    cfg,
                    [{**stalled, "source_timestamp": f"2026-08-07T12:00:0{scan}Z"}],
                    [],
                    [],
                )
            self.assertEqual(1, conn.execute("select count(*) from market_admission_states").fetchone()[0])
            self.assertGreaterEqual(
                conn.execute("select count(*) from market_admission_transitions").fetchone()[0],
                1,
            )
            self.assertEqual(0, conn.execute("select count(*) from improvement_tasks").fetchone()[0])
            self.assertEqual(0, conn.execute("select count(*) from market_hunter_directives").fetchone()[0])

    def test_report_sample_is_bounded_and_counts_touched_vs_persistent(self):
        cfg = settings()
        cfg["market_admission"].update(
            {"diagnostics_enabled": False, "report_state_limit": 100}
        )
        candidates = [
            global_candidate(
                inst_id=f"KOREA_EXCHANGE:TEST-{index}",
                proxy_symbol=f"TEST-{index}",
                signal_lineage_key=f"GLOBAL_ACTIVE|test|lineage-{index}",
            )
            for index in range(105)
        ]
        with memory_db() as conn:
            first = market_admission.run_market_admission_monitor(conn, cfg, candidates, [], [])
            payload = json.loads(market_admission.REPORT_JSON.read_text(encoding="utf-8"))
            self.assertEqual(105, first["summary"]["touched_state_count"])
            self.assertEqual(105, first["summary"]["persistent_state_count"])
            self.assertEqual(100, payload["summary"]["reported_state_count"])
            self.assertEqual(5, payload["summary"]["states_truncated"])
            self.assertEqual(100, len(payload["states"]))
            self.assertEqual(100, len(first["states"]))
            self.assertEqual(5, first["omitted_state_count"])

            second = market_admission.run_market_admission_monitor(
                conn, cfg, [candidates[0]], [], []
            )
            self.assertEqual(1, second["summary"]["touched_state_count"])
            self.assertEqual(105, second["summary"]["persistent_state_count"])

    def test_closed_session_is_scheduled_wait_without_retry_counters(self):
        closed = global_candidate(session_status="closed")
        with memory_db() as conn:
            for scan in range(2):
                report = market_admission.run_market_admission_monitor(
                    conn,
                    settings(),
                    [{**closed, "source_timestamp": f"2026-08-07T12:00:0{scan}Z"}],
                    [],
                    [],
                )
            state = report["states"][0]
        self.assertEqual("scheduled_wait", state["health_status"])
        self.assertEqual("market_closed", state["blocker_code"])
        self.assertEqual(0, state["attempts"])
        self.assertEqual(0, state["eligible_scans"])
        self.assertEqual(0, state["stalled_eligible_scans"])
        self.assertEqual(0, state["consecutive_failures"])

    def test_static_reference_inventory_is_terminal_in_current_and_lifetime_report(self):
        reference = {
            "venue": "B3",
            "inst_id": "B3:STATIC:REGISTER",
            "trade_type": "official_market_catalog",
            "market_surface": "instrument_register",
            "direction": "watch_only",
            "data_status": "reachable",
            "session_status": "unknown",
            "freshness_state": "reference_static",
        }
        with memory_db() as conn:
            report = market_admission.run_market_admission_monitor(
                conn, settings(), [], [], [reference]
            )
            state = report["states"][0]
        self.assertEqual("terminal_reference", state["health_status"])
        self.assertEqual("terminal_reference", state["terminal_class"])
        self.assertEqual(0, state["attempts"])
        self.assertEqual(1, report["summary"]["by_terminal_class"]["terminal_reference"])
        self.assertEqual(
            1,
            report["summary"]["persistent_by_terminal_class"]["terminal_reference"],
        )
        self.assertEqual(1, report["summary"]["lifetime_terminal_reference_count"])

    def test_reviewed_episode_cannot_borrow_an_older_episode_label(self):
        base = global_candidate()
        admission_key = market_admission.admission_key_for(base)
        old = {
            **base,
            "admission_key": admission_key,
            "admission_episode_id": "episode-old",
            "episode_id": "episode-old",
        }
        paper_review = {
            "decision": "approve_paper_trade",
            "learned_score": 60.0,
            "hard_blocks": [],
        }
        with memory_db() as conn:
            trade_id = open_paper_trade(conn, old, paper_review, settings=settings())
            conn.execute(
                "update paper_trades set status='closed',pnl_bps=12,close_measurement_status='valid' where id=?",
                (trade_id,),
            )
            conn.execute(
                """
                insert into paper_trade_outcomes(
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    measurement_status,admission_key,admission_episode_id
                ) values(?,60,'2026-08-07T12:00:00Z',75,12,'{}','valid',?,?)
                """,
                (trade_id, admission_key, "episode-old"),
            )
            conn.commit()
            old_report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [old],
                [{"candidate": old, "review": paper_review, "opportunity_id": 98}],
                [],
            )
            self.assertEqual("paper_evaluated", old_report["states"][0]["current_stage"])
            current = {
                **base,
                "admission_key": admission_key,
                "admission_episode_id": "episode-new",
                "episode_id": "episode-new",
                "paper_admission": {
                    "admission_key": admission_key,
                    "episode_id": "episode-new",
                    "strategy_lineage": base["signal_lineage_key"],
                },
            }
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [base],
                [{"candidate": current, "review": paper_review, "opportunity_id": 99}],
                [],
            )
            state = report["states"][0]
        self.assertEqual("episode-new", state["current_episode_id"])
        self.assertEqual("paper_eligible", state["current_stage"])
        self.assertEqual("paper_evaluated", state["highest_stage"])
        self.assertEqual("none", state["details"]["attribution_scope"])
        self.assertEqual(0, state["details"]["valid_labels"])
        self.assertEqual(0, report["summary"]["paper_evaluated_count"])
        self.assertEqual(1, report["summary"]["lifetime_paper_evaluated_count"])

    def test_bounded_paper_evaluated_requires_queue_link_to_exact_trade(self):
        base = global_candidate()
        admission_key = market_admission.admission_key_for(base)
        episode_id = "episode-exact-artifact"
        exact = {
            **base,
            "admission_key": admission_key,
            "admission_episode_id": episode_id,
            "episode_id": episode_id,
        }
        paper_review = {
            "decision": "approve_paper_trade",
            "learned_score": 60.0,
            "hard_blocks": [],
        }
        bounded = settings()
        bounded["market_admission"]["paper_queue_enabled"] = True
        with memory_db() as conn:
            trade_id = open_paper_trade(conn, exact, paper_review, settings=settings())
            conn.execute(
                """
                update paper_trades
                set status='closed',pnl_bps=12,close_measurement_status='valid'
                where id=?
                """,
                (trade_id,),
            )
            conn.execute(
                """
                insert into paper_trade_outcomes(
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    measurement_status,admission_key,admission_episode_id
                ) values(?,60,'2026-08-07T12:00:00Z',75,12,'{}','valid',?,?)
                """,
                (trade_id, admission_key, episode_id),
            )
            conn.execute(
                """
                insert into paper_admission_queue(
                    queue_id,admission_key,episode_id,evidence_fingerprint,
                    evidence_observed_at,lane,status,priority,venue,inst_id,
                    market_surface,lineage_root,direction,route_status,
                    candidate_json,eligibility_json,enqueued_at,updated_at,
                    paper_trade_id
                ) values(?,?,?,?,?,'discovery','completed_valid',0,?,?,?,?,?,
                         'standard',?,'{}',?,?,?)
                """,
                (
                    "queue-exact-artifact",
                    admission_key,
                    episode_id,
                    "fingerprint-exact-artifact",
                    "2026-08-07T12:00:00Z",
                    exact["venue"],
                    exact["inst_id"],
                    exact["trade_type"],
                    exact["signal_lineage_key"],
                    exact["direction"],
                    json.dumps(exact, sort_keys=True),
                    "2026-08-07T12:00:00Z",
                    "2026-08-07T12:00:00Z",
                    trade_id + 1000,
                ),
            )
            conn.commit()

            mislinked = market_admission.run_market_admission_monitor(
                conn,
                bounded,
                [exact],
                [{"candidate": exact, "review": paper_review}],
                [],
            )
            conn.execute(
                "update paper_admission_queue set paper_trade_id=? where queue_id=?",
                (trade_id, "queue-exact-artifact"),
            )
            conn.commit()
            linked = market_admission.run_market_admission_monitor(
                conn,
                bounded,
                [exact],
                [{"candidate": exact, "review": paper_review}],
                [],
            )

        self.assertEqual("paper_eligible", mislinked["states"][0]["current_stage"])
        self.assertEqual("paper_evaluated", linked["states"][0]["current_stage"])


if __name__ == "__main__":
    unittest.main()
