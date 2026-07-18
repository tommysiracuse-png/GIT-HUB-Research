import unittest

from src.frontier_data_quality import (
    paper_only_bybit_health_probe_trace,
    paper_only_bybit_health_route_candidates,
    paper_only_enrich_venue_health_row,
)


class TestFrontierDataQualityHealthRowFields(unittest.TestCase):
    def test_bybit_trace_marks_403_fallback_success(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        candidates = paper_only_bybit_health_route_candidates(url)

        trace = paper_only_bybit_health_probe_trace(
            url,
            route_reports=[
                {"url": candidates[0], "status": 403, "reachable": False, "error": "Forbidden"},
                {"url": candidates[2], "status": 200, "reachable": True},
            ],
        )

        self.assertEqual(trace["requested_route"], url)
        self.assertEqual(trace["adapter_route_used"], candidates[2])
        self.assertTrue(trace["fallback_applied"])
        self.assertTrue(trace["reachable_via_fallback"])
        self.assertEqual(trace["downgrade_reason"], "primary_access_denied_fallback_used")
        self.assertEqual(trace["status_chain"][0]["status"], "403")
        self.assertTrue(trace["status_chain"][1]["reachable"])

    def test_enrich_health_row_promotes_bybit_fallback_reachability(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
        candidates = paper_only_bybit_health_route_candidates(url)
        row = {"venue": "bybit", "reachable": False, "health_score": 0.0}

        enriched = paper_only_enrich_venue_health_row(
            row,
            probe_url=url,
            route_reports=[
                {"url": candidates[0], "status": 403, "reachable": False},
                {"url": candidates[-1], "status": 200, "reachable": True},
            ],
        )

        for field in (
            "requested_route",
            "adapter_route_used",
            "fallback_applied",
            "status_chain",
            "reachable_via_fallback",
            "downgrade_reason",
        ):
            self.assertIn(field, enriched)
        self.assertTrue(enriched["reachable"])
        self.assertTrue(enriched["reachable_via_fallback"])

    def test_non_bybit_health_row_is_unchanged(self):
        row = {"venue": "binance", "reachable": True, "health_score": 1.0}
        url = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"

        enriched = paper_only_enrich_venue_health_row(
            row,
            probe_url=url,
            route_reports=[{"url": url, "status": 200, "reachable": True}],
        )

        self.assertEqual(enriched, row)

    def test_bybit_route_candidates_include_orderbook_fallback_for_linear_canary(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        candidates = paper_only_bybit_health_route_candidates(url)

        self.assertEqual(candidates[0], url)
        self.assertTrue(any("/v5/market/orderbook" in candidate for candidate in candidates))
        self.assertTrue(any("api.bytick.com" in candidate for candidate in candidates))


if __name__ == "__main__":
    unittest.main()
