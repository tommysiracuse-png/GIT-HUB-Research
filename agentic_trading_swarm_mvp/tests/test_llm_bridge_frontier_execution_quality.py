import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from llm_bridge import _compact_frontier_execution_quality


class FrontierExecutionQualityPacketTests(unittest.TestCase):
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
