import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import strategy_reliability


class PaperLineageContextTests(unittest.TestCase):
    def test_blocks_inherited_boost_without_independent_target_context_evidence(self):
        candidate = {
            "lineage_id": "lineage-a",
            "venue": "GATE",
            "trade_type": "spot",
            "direction": "long",
            "holding_profile": "swing",
            "paper_context_observation_count": 0,
            "paper_context_win_count": 4,
        }

        record = strategy_reliability.paper_lineage_context(candidate)

        self.assertEqual(record["context_key"], "lineage-a|gate|spot|long|swing")
        self.assertFalse(record["has_independent_target_context_observations"])
        self.assertFalse(record["inherited_score_boost_allowed"])

    def test_requires_positive_target_context_quality_and_observations(self):
        candidate = {
            "strategy": "route_rich_frontier_long_filter",
            "venue": "COINBASE",
            "trade_type": "spot",
            "direction": "long",
            "expected_hold_minutes": 90,
            "target_context_paper_observation_count": 2,
            "target_context_paper_win_count": 1,
        }

        record = strategy_reliability.paper_lineage_context(candidate, minimum_threshold=2)

        self.assertEqual(record["venue"], "coinbase")
        self.assertEqual(record["holding_profile"], "intraday")
        self.assertEqual(
            record["context_key"],
            "route_rich_frontier_long_filter|coinbase|spot|long|intraday",
        )
        self.assertTrue(record["has_target_context_win_quality"])
        self.assertTrue(record["inherited_score_boost_allowed"])
