import unittest

from src.route_intelligence import build_build_governor_fields, build_route_requirements_report


class LlmStatePacketRouteIntelligenceTests(unittest.TestCase):
    def test_proxy_only_route_is_exposed_with_proxy_semantics(self) -> None:
        report = build_route_requirements_report(
            [
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT",
                    "market_key": "frontier_conditional_spot_short",
                    "direction": "short",
                    "route_blockers": ["spot_borrow"],
                    "paper_testable_proxy": "hedged_perp_proxy",
                }
            ]
        )

        row = report["routes"][0]
        self.assertEqual("paper_testable_via_proxy", row["route_status"])
        self.assertEqual("proxy_only", row["paper_feasibility"])
        self.assertEqual("hedged_perp_proxy", row["paper_proxy_route"])
        self.assertTrue(row["paper_proxy_not_live_equivalent"])

    def test_direct_route_remains_direct_feasible(self) -> None:
        report = build_route_requirements_report(
            [
                {
                    "venue": "OKX",
                    "inst_id": "OKX:ETH-USDT-SWAP",
                    "market_key": "funding_capture",
                    "direction": "long",
                    "instrument_type": "perpetual_swap",
                }
            ]
        )

        row = report["routes"][0]
        self.assertEqual("paper_observation_only", row["route_status"])
        self.assertEqual("direct_feasible", row["paper_feasibility"])
        self.assertEqual("not_applicable", row["paper_proxy_route"])
        self.assertFalse(row["paper_proxy_not_live_equivalent"])

    def test_build_governor_reports_feasibility_counts(self) -> None:
        fields = build_build_governor_fields(
            [
                {
                    "venue": "OKX",
                    "inst_id": "OKX:BTC-USDT",
                    "market_key": "frontier_conditional_spot_short",
                    "direction": "short",
                    "route_blockers": ["spot_borrow"],
                    "paper_testable_proxy": "hedged_perp_proxy",
                },
                {
                    "venue": "GATE",
                    "inst_id": "GATE:ARC_USDT",
                    "market_key": "frontier_conditional_spot_short",
                    "direction": "short",
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "venue": "OKX",
                    "inst_id": "OKX:ETH-USDT-SWAP",
                    "market_key": "funding_capture",
                    "direction": "long",
                    "instrument_type": "perpetual_swap",
                },
            ]
        )

        summary = fields["paper_feasibility_summary"]
        self.assertEqual(1, summary["counts_by_feasibility"]["proxy_only"])
        self.assertEqual(1, summary["counts_by_feasibility"]["blocked"])
        self.assertEqual(1, summary["counts_by_feasibility"]["direct_feasible"])
        self.assertEqual(1, summary["proxy_routes"]["hedged_perp_proxy"])
