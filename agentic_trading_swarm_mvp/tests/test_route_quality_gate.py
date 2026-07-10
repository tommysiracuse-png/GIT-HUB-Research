import unittest

from src.route_intelligence import build_conditional_paper_quality_gate


class ConditionalPaperQualityGateTests(unittest.TestCase):
    def test_gates_unresolved_borrow_and_shadow_quality(self):
        opportunities = [
            {
                "venue": "MEXC",
                "inst_id": "MEXC:AAVEUSDT",
                "direction": "short_frontier_spot",
                "route_status": "conditional",
                "route_blockers": ["spot_borrow"],
                "anomaly_flags": ["simulated_slippage_exceeds_edge"],
                "edge_bps_estimate": 0.0,
                "score": 43.257,
            },
            {
                "venue": "OKX_SPOT",
                "inst_id": "OKX_SPOT:SEI-USDT",
                "direction": "long_frontier_spot",
                "route_status": "standard",
                "quality_action": "shadow_only",
                "anomaly_flags": ["empty_book"],
                "edge_bps_estimate": 75.256,
                "score": 95.253,
            },
            {
                "venue": "GATE",
                "inst_id": "GATE:CRCLX_USDT",
                "direction": "long_frontier_spot",
                "route_status": "standard",
                "quality_action": "normal",
                "anomaly_flags": [],
                "edge_bps_estimate": 78.897,
                "score": 100.0,
            },
        ]

        summary = build_conditional_paper_quality_gate(opportunities)

        self.assertTrue(summary["paper_only"])
        self.assertEqual(summary["gate_count"], 2)
        self.assertEqual(
            summary["reason_counts"]["unconfirmed_short_or_borrow_route"], 1
        )
        self.assertEqual(
            summary["reason_counts"]["market_data_quality_shadow_only"], 1
        )
        self.assertIn(
            "slippage_exceeds_nonpositive_edge", summary["reason_counts"]
        )
        gated_ids = {item["inst_id"] for item in summary["top_examples"]}
        self.assertEqual(
            gated_ids, {"MEXC:AAVEUSDT", "OKX_SPOT:SEI-USDT"}
        )

    def test_accepts_dict_route_blockers(self):
        summary = build_conditional_paper_quality_gate(
            [
                {
                    "instrument": "COINBASE:ADA-USDT",
                    "direction": "short_frontier_spot",
                    "route_status": "conditional",
                    "route_blockers": {"spot_borrow": "missing"},
                    "net_edge_after_borrow_cost_bps": -26.0,
                }
            ]
        )

        self.assertEqual(summary["gate_count"], 1)
        self.assertEqual(summary["top_examples"][0]["inst_id"], "COINBASE:ADA-USDT")
        self.assertIn("spot_borrow", summary["top_examples"][0]["route_blockers"])


if __name__ == "__main__":
    unittest.main()
