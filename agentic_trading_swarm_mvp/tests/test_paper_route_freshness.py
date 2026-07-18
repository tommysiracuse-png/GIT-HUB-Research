import unittest

from src.frontier_crypto_adapter import paper_only_route_freshness_gate


class PaperRouteFreshnessGateTests(unittest.TestCase):
    def test_selects_fresh_route_over_stale_primary(self):
        result = paper_only_route_freshness_gate(
            [
                {"venue": "primary", "price": 100.0, "latency_ms": 2.0, "quote_age_ms": 900.0},
                {"venue": "secondary", "price": 100.5, "latency_ms": 1.0, "quote_age_ms": 120.0},
            ],
            quote_stale_threshold_ms=750.0,
        )

        self.assertFalse(result["suppress_fill"])
        self.assertEqual(result["selected_route"]["venue"], "secondary")
        self.assertEqual(len(result["eligible_routes"]), 1)

    def test_suppresses_fill_when_all_routes_stale(self):
        result = paper_only_route_freshness_gate(
            [
                {"venue": "primary", "price": 100.0, "latency_ms": 2.0, "quote_age_ms": 900.0},
                {"venue": "secondary", "price": 100.5, "latency_ms": 1.0, "quote_age_ms": 880.0},
            ],
            quote_stale_threshold_ms=750.0,
        )

        self.assertTrue(result["suppress_fill"])
        self.assertTrue(result["route_stale_no_fill"])
        self.assertIsNone(result["selected_route"])

