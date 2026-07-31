import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_order_router import frontier_shadow_filter_reason
from strategy_reliability import paper_frontier_execution_quality_gate_record


def _base_candidate() -> dict:
    return {
        "market_key": "OKX_SPOT",
        "market_surface": "OKX_SPOT",
        "venue": "OKX",
        "signal_key": "OKX_SPOT|frontier_crypto_venue_map|short_frontier_spot|conditional",
        "trade_type": "short_frontier_spot",
        "strategy": "frontier_crypto_venue_map",
        "direction": "short",
    }


class PaperFrontierExecutionQualityGateRecordTests(unittest.TestCase):
    def test_rejects_low_quality_okx_frontier_short_context(self) -> None:
        candidate = _base_candidate()
        candidate.update(
            {
                "route_count": 1,
                "spread_bps": 18.4,
                "book_age_seconds": 31,
                "top_of_book_notional_usd": 8000,
            }
        )

        record = paper_frontier_execution_quality_gate_record(candidate)

        self.assertIsNotNone(record)
        self.assertFalse(record["eligible"])
        failed_names = {item["name"] for item in record["failed_checks"]}
        self.assertIn("route_richness", failed_names)
        self.assertIn("spread_bps", failed_names)
        self.assertIn("freshness_seconds", failed_names)
        self.assertIn("book_depth_usd", failed_names)
        self.assertEqual(record["paper_score_multiplier"], 0.0)

    def test_marks_favorable_context_when_quality_signals_confirmed(self) -> None:
        candidate = _base_candidate()
        candidate.update(
            {
                "route_count": 3,
                "spread_bps": 3.2,
                "book_age_seconds": 2.5,
                "top_of_book_notional_usd": 75000,
            }
        )

        record = paper_frontier_execution_quality_gate_record(candidate)

        self.assertIsNotNone(record)
        self.assertTrue(record["eligible"])
        self.assertTrue(record["favorable_context"])
        self.assertEqual(record["paper_score_multiplier"], 1.0)

    def test_non_target_candidate_returns_none(self) -> None:
        candidate = {
            "market_key": "BINANCE_SPOT",
            "signal_key": "BINANCE_SPOT|mean_reversion|long|conditional",
            "trade_type": "long_mean_reversion",
        }

        self.assertIsNone(paper_frontier_execution_quality_gate_record(candidate))


class PaperFrontierExecutionQualityRouterTests(unittest.TestCase):
    def test_router_surfaces_execution_quality_failure_as_shadow_reason(self) -> None:
        candidate = _base_candidate()
        candidate.update(
            {
                "route_count": 1,
                "spread_bps": 22.0,
                "book_age_seconds": 40.0,
            }
        )

        reason = frontier_shadow_filter_reason(candidate)

        self.assertIsNotNone(reason)
        self.assertEqual(reason["reason"], "paper_frontier_execution_quality_gate")
        self.assertFalse(reason["paper_fill_allowed"])
        self.assertTrue(reason["failed_checks"])

    def test_quality_gate_can_be_disabled_for_rollback(self) -> None:
        candidate = _base_candidate()
        candidate.update({"route_count": 1, "spread_bps": 22.0, "book_age_seconds": 40.0})

        reason = frontier_shadow_filter_reason(candidate, config={"paper_frontier_execution_quality_gate_enabled": False})

        self.assertIsNone(reason)
