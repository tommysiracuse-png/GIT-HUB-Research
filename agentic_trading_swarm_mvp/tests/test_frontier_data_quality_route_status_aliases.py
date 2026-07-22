import unittest

from src.frontier_data_quality import paper_only_route_quality_record


class PaperOnlyRouteStatusAliasTests(unittest.TestCase):
    def test_blocked_route_alias_marks_candidate_ineligible(self):
        result = paper_only_route_quality_record(
            route_status="blocked_route",
            observed_at="2026-07-22T00:00:00Z",
            as_of="2026-07-22T00:00:01Z",
        )

        self.assertEqual(result.get("route_status"), "blocked")
        self.assertTrue(result.get("paper_ineligible"))
        self.assertEqual(result.get("paper_decision"), "blocked")
        self.assertEqual(result.get("simulated_slippage_tier"), "blocked")

    def test_rate_limited_alias_from_status_packet_dict_degrades_cleanly(self):
        result = paper_only_route_quality_record(
            route_status={"route_status": "rate-limited"},
            observed_at="2026-07-22T00:00:00Z",
            as_of="2026-07-22T00:00:01Z",
        )

        self.assertEqual(result.get("route_status"), "limited")
        self.assertFalse(result.get("paper_ineligible"))
        self.assertEqual(result.get("paper_decision"), "degraded")
        self.assertEqual(result.get("simulated_slippage_tier"), "elevated")


if __name__ == "__main__":
    unittest.main()
