import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_order_router


class PaperOrderRouterLineageContextTests(unittest.TestCase):
    def test_apply_route_feasibility_metadata_adds_lineage_partition_fields(self):
        candidate = {
            "lineage_id": "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975",
            "venue": "KRAKEN",
            "trade_type": "spot",
            "direction": "long",
            "holding_profile": "overnight",
            "paper_context_observation_count": 3,
            "paper_context_win_count": 2,
            "route_status": "executable",
        }

        annotated = paper_order_router._apply_route_feasibility_metadata(candidate)

        self.assertEqual(
            annotated["paper_lineage_context_key"],
            "strategy_lab|route_rich_frontier_long_filter_2942c975|kraken|spot|long|overnight",
        )
        self.assertTrue(annotated["paper_lineage_inherited_boost_allowed"])
        self.assertEqual(annotated["paper_lineage_context"]["target_context_observation_count"], 3)

    def test_candidate_reference_carries_lineage_context_when_present(self):
        candidate = {
            "lineage_id": "lineage-a",
            "venue": "MEXC",
            "trade_type": "perp",
            "direction": "short",
            "expected_hold_minutes": 45,
        }

        reference = paper_order_router._candidate_reference(candidate)

        self.assertEqual(reference["paper_lineage_context_key"], "lineage-a|mexc|perp|short|intraday")
        self.assertEqual(reference["paper_lineage_context"]["holding_profile"], "intraday")

