import pathlib
import sqlite3
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import llm_bridge
from llm_bridge import _compact_frontier_execution_quality
from settings import DEFAULT_SETTINGS
from storage import init_db, utc_now


class FrontierExecutionQualityPacketTests(unittest.TestCase):
    def test_state_packet_exposes_short_frontier_route_outcomes_to_the_route_hunter(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            """
            insert into signal_stats
                (signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "SYNTHETIC_RESEARCH|MEXC|frontier_crypto_venue_map|short_frontier_spot|conditional",
                19,
                5,
                -41.539,
                0.263,
                0.0,
                utc_now(),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            old_json, old_md = llm_bridge.STATE_JSON, llm_bridge.STATE_MD
            llm_bridge.STATE_JSON = pathlib.Path(tmp) / "state.json"
            llm_bridge.STATE_MD = pathlib.Path(tmp) / "state.md"
            try:
                packet = llm_bridge.write_llm_state_packet(
                    conn,
                    {
                        "route_requirement_candidates": [
                            {
                                "venue": "MEXC",
                                "inst_id": "MEXC:BTC-USDT",
                                "trade_type": "frontier_crypto_venue_map",
                                "direction": "short_frontier_spot",
                                "score": 64.0,
                            },
                            {
                                "venue": "OKX",
                                "inst_id": "OKX:BTC-USDT-SWAP",
                                "trade_type": "perp_funding_basis",
                                "direction": "short_perp_long_spot",
                                "score": 58.0,
                            },
                        ]
                    },
                    DEFAULT_SETTINGS,
                )
            finally:
                llm_bridge.STATE_JSON, llm_bridge.STATE_MD = old_json, old_md

        outcome_diagnostics = packet["short_frontier_spot_route_outcomes"]
        self.assertTrue(outcome_diagnostics["paper_only"])
        self.assertEqual(1, outcome_diagnostics["route_count"])
        route = outcome_diagnostics["routes"][0]
        self.assertEqual("MEXC", route["venue"])
        self.assertEqual("weak_paper_outcome", route["outcome_status"])
        self.assertEqual("diagnose_and_down_rank_only", route["ranking_input"]["ranking_action"])
        self.assertFalse(route["entry_blocked"])
        route_summaries = packet["paper_route_requirement_summaries"]
        self.assertTrue(route_summaries["read_only"])
        self.assertEqual(2, route_summaries["candidate_count"])
        self.assertEqual(0.0, route_summaries["candidates"][0]["ranking_annotation"]["score_adjustment"])

    def test_state_packet_exposes_frontier_gap_summary_to_market_scout(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        with tempfile.TemporaryDirectory() as tmp:
            old_json, old_md = llm_bridge.STATE_JSON, llm_bridge.STATE_MD
            state_json = pathlib.Path(tmp) / "state.json"
            state_md = pathlib.Path(tmp) / "state.md"
            llm_bridge.STATE_JSON = state_json
            llm_bridge.STATE_MD = state_md
            try:
                packet = llm_bridge.write_llm_state_packet(
                    conn,
                    {
                        "frontier_crypto_venues": {
                            "summary": {
                                "candidate_count": 40,
                                "active_paper_review_candidate_count": 9,
                                "candidate_activity": {
                                    "active_paper_review_candidates": 9,
                                    "regional_admitted_candidates": 0,
                                    "route_feasibility_shadow_candidates": 3,
                                },
                                "by_quote_normalization": {
                                    "unsupported_quote": 4,
                                    "external_fx_reference": 47,
                                    "missing_same_venue_stablecoin_reference": 162,
                                },
                                "blocked_venues": ["BITSO"],
                                "degraded_venues": ["MEXC"],
                                "expansion_map": {
                                    "depth_enriched_rate": 0.3009,
                                    "unknown_quality_count": 11,
                                },
                            }
                        }
                    },
                    DEFAULT_SETTINGS,
                )
                markdown = state_md.read_text(encoding="utf-8")
            finally:
                llm_bridge.STATE_JSON, llm_bridge.STATE_MD = old_json, old_md

        gap_summary = packet["frontier_gap_summary"]
        self.assertTrue(gap_summary["paper_only"])
        self.assertTrue(gap_summary["read_only"])
        self.assertEqual(40, gap_summary["frontier_candidates"])
        self.assertEqual(9, gap_summary["active_paper_review_candidates"])
        self.assertEqual(30.09, gap_summary["depth_enrichment_rate_pct"])
        self.assertEqual(162, gap_summary["quote_gap_counts"]["needs_same_venue_stablecoin_reference"])
        self.assertEqual("request_quote_adapter", gap_summary["priority_gaps"][0]["recommended_request"])
        self.assertIn("Frontier gap summary", markdown)

    def test_compact_frontier_execution_quality_counts_route_and_quote_failures(self):
        research_worker = {
            "candidates": [
                {
                    "venue_or_source": "Bybit",
                    "asset_or_event": "BTCUSDT",
                    "surface_type_raw": "crypto global spot public market data",
                    "tradability_guess": "directly_tradable",
                    "data_access_type": "public_no_key",
                    "route_blockers": [],
                    "quote_age_ms": 2000,
                    "normalized_spread_bps": 8,
                    "top_of_book_depth_notional": 5000,
                    "required_paper_notional": 1000,
                },
                {
                    "venue_or_source": "Bitso",
                    "asset_or_event": "MXN crypto pairs",
                    "surface_type_raw": "crypto regional fiat spot rails",
                    "tradability_guess": "route_needed",
                    "data_access_type": "public_no_key",
                    "route_blockers": ["venue_api_access"],
                    "quote_age_ms": 3000,
                    "normalized_spread_bps": 10,
                    "top_of_book_depth_notional": 4000,
                    "required_paper_notional": 1000,
                },
                {
                    "venue_or_source": "CME Group",
                    "asset_or_event": "rates futures",
                    "surface_type_raw": "futures and options exchange",
                    "tradability_guess": "route_needed",
                    "data_access_type": "broker_account",
                    "route_blockers": ["futures_account"],
                    "quote_age_ms": 30000,
                    "normalized_spread_bps": 30,
                    "top_of_book_depth_notional": 500,
                    "required_paper_notional": 1000,
                },
            ]
        }

        compact = _compact_frontier_execution_quality(research_worker)

        self.assertTrue(compact["paper_only"])
        self.assertEqual(compact["candidate_count"], 3)
        self.assertEqual(compact["route_feasibility_counts"]["standard"], 1)
        self.assertEqual(compact["route_feasibility_counts"]["conditional"], 1)
        self.assertEqual(compact["route_feasibility_counts"]["blocked"], 1)
        self.assertEqual(compact["hold_candidate_count"], 2)
        self.assertEqual(compact["failing_gate_counts"]["route_feasibility_state"], 2)
        self.assertEqual(compact["failing_gate_counts"]["quote_age_ms"], 1)
        self.assertEqual(compact["failing_gate_counts"]["normalized_spread_bps"], 1)
        self.assertEqual(compact["failing_gate_counts"]["top_of_book_depth_notional"], 1)
        self.assertEqual(compact["priority_watchlist"][0]["venue_or_source"], "Bitso")

    def test_compact_frontier_execution_quality_uses_report_threshold_overrides(self):
        research_worker = {
            "execution_quality_review": {
                "quote_age_ms_max": 9000,
                "normalized_spread_bps_max": 15,
                "normalized_spread_bps_max_short_frontier_spot": 10,
                "minimum_depth_notional": 2500,
                "hold_on_missing_metrics": False,
            },
            "candidates": [
                {
                    "venue_or_source": "OKX_SPOT",
                    "asset_or_event": "BTCUSDT short paper reference",
                    "surface_type_raw": "crypto frontier spot public market data",
                    "direction": "short",
                    "tradability_guess": "directly_tradable",
                    "data_access_type": "public_no_key",
                    "quote_age_ms": 10000,
                    "normalized_spread_bps": 12,
                    "top_of_book_depth_notional": 3000,
                }
            ],
        }

        compact = _compact_frontier_execution_quality(research_worker)

        self.assertEqual(compact["admission_gate"]["quote_age_ms_max"], 9000)
        self.assertEqual(compact["admission_gate"]["normalized_spread_bps_max"], 15)
        self.assertEqual(compact["admission_gate"]["normalized_spread_bps_max_short_frontier_spot"], 10)
        self.assertEqual(compact["admission_gate"]["minimum_depth_notional"], 2500)
        self.assertEqual(compact["hold_candidate_count"], 1)
