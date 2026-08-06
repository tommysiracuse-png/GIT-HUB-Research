import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from paper_order_router import apply_frontier_paper_guard, frontier_shadow_filter_reason
from strategy_reliability import _route_status, frontier_route_feasibility_record


def _candidate(**overrides):
    candidate = {
        "market_surface": "frontier_crypto_venue_map",
        "signal_family": "frontier_crypto_venue_map",
        "direction": "short_frontier_spot",
        "trade_type": "conditional",
        "symbol": "PEPE/USDT",
        "venue": "OKX",
        "score": 68.0,
        "edge_bps_estimate": 10.0,
        "gross_edge_bps_estimate": 30.0,
        "estimated_round_trip_cost_bps": 20.0,
    }
    candidate.update(overrides)
    return candidate


class RouteFeasibilityRuntimeIntegrationTests(unittest.TestCase):
    def test_assumption_backed_short_with_route_blockers_is_shadow_only(self):
        candidate = _candidate(
            venue="PAPER_SIM_VENUE",
            score=80.0,
            paper_short_simulation_allowed=True,
            borrow_inventory_assumption="fixed_conservative_inventory",
            borrow_cost_assumption={"bps": 25.0, "model": "paper_stress"},
            venue_capabilities={
                "supports_spot_short": True,
                "supports_margin_spot": True,
                "supports_borrow_check": True,
            },
            route_blockers=["spot_borrow"],
            execution_feasibility={
                "status": "conditional",
                "route_blockers": ["spot_borrow"],
            },
        )

        guarded = apply_frontier_paper_guard(candidate)

        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual("paper_net_edge_guard", guarded["candidate_reject_reason"])

    def test_pretrade_route_metadata_remains_diagnostic_without_blockers(self):
        candidate = _candidate(
            paper_route_eligibility={
                "paper_only": True,
                "suppressed": False,
                "execution_eligibility": "eligible",
            },
            venue_capabilities={
                "supports_spot_short": False,
                "supports_margin_spot": False,
                "supports_borrow_check": False,
            },
            paper_short_simulation_allowed=True,
            borrowable=True,
            borrow_cost_bps=4.0,
            margin_eligible=True,
        )

        guarded = apply_frontier_paper_guard(candidate)

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertEqual("blocked", guarded["execution_eligibility"])
        self.assertEqual(
            "infeasible_for_paper",
            guarded["paper_feasibility_status"],
        )
        self.assertEqual("unsupported", guarded["route_intelligence_status"])
        self.assertEqual(
            "quarantined_route_unavailable", guarded["candidate_status"]
        )
        self.assertEqual(0.0, guarded["rank_contribution_cap"])
        self.assertTrue(guarded["required_capabilities"])
        self.assertTrue(guarded["paper_route_notes"])

    def test_direct_pretrade_path_keeps_missing_venue_metadata_diagnostic(self):
        candidate = _candidate(
            venue="UNKNOWN_FRONTIER",
            paper_short_simulation_allowed=True,
            borrowable=True,
            borrow_cost_bps=4.0,
            margin_eligible=True,
            execution_feasibility={"status": "standard"},
        )

        guarded = apply_frontier_paper_guard(candidate)

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertEqual("unknown", guarded["route_intelligence_status"])
        self.assertEqual("route_needs_confirmation", guarded["candidate_status"])
        self.assertEqual(0.2, guarded["rank_contribution_cap"])
        self.assertEqual(
            {
                "supports_spot_short",
                "supports_margin_spot",
                "supports_borrow_check",
            },
            set(guarded["required_capabilities"]),
        )

    def test_direct_pretrade_path_blocks_carry_with_unsupported_venue_metadata(self):
        candidate = {
            "venue": "TEST_SPOT",
            "trade_type": "synthetic_carry",
            "direction": "short_perp_long_spot",
            "hedge_venue": "TEST_SPOT",
            "hedge_instrument": "BTC-USDT",
            "fee_model": "paper_conservative_v1",
            "paper_leg_mapping_valid": True,
            "venue_capabilities": {
                "supports_spot_long": True,
                "supports_perpetuals": False,
                "supports_basis_carry": False,
            },
        }

        guarded = apply_frontier_paper_guard(candidate)

        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual(
            "paper_route_eligibility_gate",
            guarded["candidate_reject_detail"]["guard"],
        )
        self.assertIn(
            "venue_synthetic_carry_capability_unconfirmed",
            guarded["candidate_reject_detail"]["blocker_reasons"],
        )

    def test_blocked_short_without_paper_proxy_is_shadow_only(self):
        candidate = _candidate(
            venue_capabilities={"paper_route_feasible": True},
            route_blockers=["spot_borrow"],
            execution_feasibility={
                "status": "blocked",
                "route_blockers": ["spot_borrow"],
            },
        )

        reason = frontier_shadow_filter_reason(candidate)
        guarded = apply_frontier_paper_guard(candidate)

        self.assertIsNotNone(reason)
        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual("paper_net_edge_guard", guarded["candidate_reject_reason"])

    def test_proxy_short_with_direct_route_blockers_is_shadow_only(self):
        candidate = _candidate(
            venue_capabilities={"paper_route_feasible": True},
            route_blockers=["spot_borrow"],
            execution_feasibility={
                "status": "blocked",
                "route_blockers": ["spot_borrow"],
                "best_route_alternative": {
                    "status": "paper_testable_proxy",
                    "route_id": "perp_hedge_proxy",
                    "venue": "OKX_SWAP",
                    "replaces_blockers": ["spot_borrow"],
                    "paper_allocation_multiplier": 0.4,
                },
            },
        )

        guarded = apply_frontier_paper_guard(candidate)
        record = frontier_route_feasibility_record(candidate)

        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual("paper_net_edge_guard", guarded["candidate_reject_reason"])

        self.assertTrue(record["paper_proxy_used"])
        self.assertEqual(record["paper_route_status"], "paper_testable_proxy")
        self.assertEqual(_route_status(candidate), "paper_testable_proxy")

    def test_strategy_reliability_record_preserves_venue_context(self):
        record = frontier_route_feasibility_record(
            _candidate(execution_feasibility={"status": "executable"}, venue="GATE")
        )

        self.assertEqual(record["venue"], "GATE")
        self.assertEqual(record["paper_route_status"], "executable")
        self.assertFalse(record["paper_proxy_used"])


if __name__ == "__main__":
    unittest.main()
