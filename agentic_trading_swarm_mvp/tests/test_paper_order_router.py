from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_order_router as router


def frontier_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "trade_type": "frontier_crypto_venue_map",
        "market_surface": "frontier_crypto_venue_map",
        "signal_key": "COINBASE|frontier_crypto_venue_map|long_frontier_spot|standard",
        "inst_id": "COINBASE:BTC-USD",
        "edge_bps_estimate": 12.0,
        "gross_edge_bps_estimate": 36.0,
        "estimated_round_trip_cost_bps": 20.0,
        "quality_action": "candidate",
        "anomaly_flags": [],
        "paper_filled": True,
        "status": "paper_filled",
    }
    candidate.update(overrides)
    return candidate


class PaperOrderRouterFrontierGuardTests(unittest.TestCase):
    def test_cost_eroded_frontier_candidate_is_retained_as_counterfactual(self) -> None:
        candidate = frontier_candidate(
            edge_bps_estimate=0.0,
            gross_edge_bps_estimate=13.044,
            estimated_round_trip_cost_bps=20.053,
        )

        guarded = router.apply_frontier_paper_guard(candidate)

        self.assertNotIn("shadow_filtered", guarded)
        self.assertTrue(guarded["paper_filled"])
        self.assertEqual("cost_swallowed_after_slippage", guarded["paper_cost_diagnostic_reason"])
        self.assertFalse(guarded["paper_cost_diagnostic"]["blocks_paper_fill"])
        self.assertTrue(candidate["paper_filled"])
        self.assertEqual(candidate["status"], "paper_filled")

    def test_slippage_anomaly_is_a_cost_diagnostic(self) -> None:
        guarded = router.apply_frontier_paper_guard(
            frontier_candidate(
                edge_bps_estimate=8.0,
                gross_edge_bps_estimate=32.0,
                estimated_round_trip_cost_bps=20.0,
                anomaly_flags=["simulated_slippage_exceeds_edge"],
            )
        )

        codes = [check["code"] for check in guarded["paper_cost_diagnostic"]["checks"]]
        self.assertIn("simulated_slippage_exceeds_edge", codes)
        self.assertNotIn("shadow_filtered", guarded)

    def test_shadow_only_quality_action_blocks_paper_fill(self) -> None:
        guarded = router.apply_frontier_paper_guard(
            frontier_candidate(
                edge_bps_estimate=90.338,
                gross_edge_bps_estimate=110.0,
                estimated_round_trip_cost_bps=20.0,
                quality_action="shadow_only",
                anomaly_flags=["depth_cliff", "empty_book"],
            )
        )

        codes = [check["code"] for check in guarded["candidate_reject_detail"]["checks"]]
        self.assertIn("quality_action_shadow_only", codes)
        self.assertEqual(guarded["paper_action"], "shadow_filtered")

    def test_verified_positive_net_frontier_candidate_remains_eligible(self) -> None:
        candidate = frontier_candidate(
            edge_bps_estimate=16.0,
            gross_edge_bps_estimate=42.0,
            estimated_round_trip_cost_bps=20.0,
            quality_action="verified",
            anomaly_flags=[],
        )

        guarded = router.apply_frontier_paper_guard(candidate)

        self.assertNotIn("shadow_filtered", guarded)
        self.assertEqual(guarded["status"], "paper_filled")
        self.assertTrue(guarded["paper_filled"])

    def test_unconfirmed_short_spot_borrow_blocks_frontier_paper_fill(self) -> None:
        guarded = router.apply_frontier_paper_guard(
            frontier_candidate(
                signal_key="GATE|frontier_crypto_venue_map|short_frontier_spot|conditional",
                inst_id="GATE:ARC_USDT",
                direction="short_frontier_spot",
                edge_bps_estimate=24.0,
                gross_edge_bps_estimate=60.0,
                estimated_round_trip_cost_bps=20.0,
                quality_action="verified",
                venue_capabilities={"paper_route_feasible": True},
                paper_short_simulation_allowed=True,
                borrowable=True,
                borrow_cost_bps=4.0,
                margin_eligible=True,
                execution_route={
                    "route_status": "conditional",
                    "missing_permissions": ["spot_borrow"],
                    "route_blockers": ["spot_borrow"],
                    "borrow_status": "required_unconfirmed",
                },
            )
        )

        codes = [check["code"] for check in guarded["candidate_reject_detail"]["checks"]]
        self.assertIn(router.SPOT_BORROW_SHADOW_CODE, codes)
        self.assertEqual(guarded["paper_fill_status"], "shadow_filtered")

    def test_net_edge_is_computed_from_gross_less_cost_not_quality_score(self) -> None:
        guarded = router.apply_frontier_paper_guard(
            frontier_candidate(
                edge_bps_estimate=1.0,
                gross_edge_bps_estimate=27.0,
                estimated_round_trip_cost_bps=22.0,
                quality_action="conditional",
                quality_score=10.0,
            )
        )

        self.assertNotIn("shadow_filtered", guarded)

    def test_confirmed_borrow_keeps_short_spot_eligible(self) -> None:
        guarded = router.apply_frontier_paper_guard(
            frontier_candidate(
                signal_key="GATE|frontier_crypto_venue_map|short_frontier_spot|conditional",
                inst_id="GATE:ARC_USDT",
                direction="short_frontier_spot",
                edge_bps_estimate=24.0,
                gross_edge_bps_estimate=60.0,
                estimated_round_trip_cost_bps=20.0,
                quality_action="verified",
                venue_capabilities={"paper_route_feasible": True},
                paper_short_simulation_allowed=True,
                borrowable=True,
                borrow_cost_bps=4.0,
                margin_eligible=True,
                execution_route={
                    "route_status": "standard",
                    "missing_permissions": [],
                    "route_blockers": [],
                    "borrow_status": "configured",
                },
            )
        )

        self.assertNotIn("shadow_filtered", guarded)

    def test_single_perpetual_funding_candidate_does_not_infer_route_dependencies(self) -> None:
        candidate = frontier_candidate(
            trade_type="perp_funding_basis",
            market_surface="funding_basis",
            signal_key="OKX|perp_funding_basis|funding_capture_long_perp|conditional",
            edge_bps_estimate=0.0,
            gross_edge_bps_estimate=4.0,
            estimated_round_trip_cost_bps=9.0,
        )

        guarded = router.apply_frontier_paper_guard(candidate)

        self.assertFalse(router.should_shadow_filter_frontier_candidate(guarded))
        self.assertNotIn("shadow_filtered", guarded)

    def test_feature_flag_can_disable_guard_for_paper_rollback(self) -> None:
        candidate = frontier_candidate(edge_bps_estimate=0.0)
        cfg = {"paper_order_router": {"frontier_shadow_guard_enabled": False}}

        guarded = router.apply_frontier_paper_guard(candidate, cfg)

        self.assertNotIn("shadow_filtered", guarded)
        self.assertEqual(guarded["status"], "paper_filled")

    def test_filter_helper_preserves_order_and_cost_diagnostics(self) -> None:
        blocked = frontier_candidate(inst_id="KRAKEN:XBTUSD", edge_bps_estimate=0.0)
        eligible = frontier_candidate(inst_id="OKX:ABC-USDT", edge_bps_estimate=12.0)

        guarded = router.filter_frontier_paper_candidates([blocked, eligible])

        self.assertEqual([row["inst_id"] for row in guarded], ["KRAKEN:XBTUSD", "OKX:ABC-USDT"])
        self.assertEqual("cost_swallowed_after_slippage", guarded[0]["paper_cost_diagnostic_reason"])
        self.assertNotIn("shadow_filtered", guarded[0])
        self.assertNotIn("shadow_filtered", guarded[1])


if __name__ == "__main__":
    unittest.main()
