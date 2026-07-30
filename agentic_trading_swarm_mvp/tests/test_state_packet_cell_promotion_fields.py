import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from paper_order_router import apply_frontier_paper_guard, frontier_shadow_filter_reason


class TestStatePacketCellPromotionFields(unittest.TestCase):
    def test_guard_annotations_include_signal_cell_metadata(self) -> None:
        candidate = {
            "signal_family": "frontier_crypto_venue_map",
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_crypto_venue_map_alpha",
            "variant": "v2",
            "venue": "GATE",
            "direction": "long",
            "paper_route_status": "executable",
            "edge_bps_estimate": 6.0,
            "gross_edge_bps_estimate": 8.0,
            "estimated_round_trip_cost_bps": 2.0,
        }

        annotated = apply_frontier_paper_guard(candidate)

        self.assertIn("paper_signal_cell", annotated)
        self.assertIn("paper_signal_cell_key", annotated)
        self.assertEqual(annotated["paper_signal_cell_key"], annotated["paper_signal_cell"]["cell_key"])
        self.assertEqual(annotated["paper_signal_cell"]["venue"], "GATE")
        self.assertEqual(annotated["paper_signal_cell"]["direction"], "long")

    def test_shadow_reason_includes_cell_reference_fields(self) -> None:
        candidate = {
            "signal_family": "frontier_crypto_venue_map",
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_crypto_venue_map_alpha",
            "variant": "v2",
            "venue": "GATE",
            "direction": "short",
            "paper_route_status": "blocked",
            "edge_bps_estimate": -4.0,
            "gross_edge_bps_estimate": 1.0,
            "estimated_round_trip_cost_bps": 3.0,
        }

        reason = frontier_shadow_filter_reason(candidate)

        self.assertIsInstance(reason, dict)
        self.assertIn("cell", reason)
        self.assertIsInstance(reason["cell"], dict)
        self.assertEqual(reason["candidate"]["paper_signal_cell_key"], reason["cell"]["cell_key"])
        self.assertEqual(reason["cell"]["direction"], "short")

    def test_cell_payload_is_json_serializable(self) -> None:
        candidate = {
            "signal_family": "frontier_crypto_venue_map",
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_crypto_venue_map_alpha",
            "variant": "v2",
            "venue": "OKX_SPOT",
            "direction": "long",
            "paper_route_status": "paper_testable_proxy",
            "edge_bps_estimate": -1.0,
        }

        reason = frontier_shadow_filter_reason(candidate)

        self.assertIsNotNone(reason)
        self.assertEqual(reason["cell"]["scope"], "paper_signal_cell_v1")
        self.assertIsInstance(reason["cell"]["cell_key"], str)
        self.assertTrue(reason["cell"]["cell_key"])


if __name__ == "__main__":
    unittest.main()
