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
    }
    candidate.update(overrides)
    return candidate


class RouteFeasibilityRuntimeIntegrationTests(unittest.TestCase):
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

    def test_blocked_short_without_paper_proxy_is_shadow_filtered(self):
        candidate = _candidate(
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
        self.assertEqual(guarded["paper_route_status"], "blocked")
        self.assertEqual(guarded["paper_route_type"], "blocked")
        self.assertFalse(guarded["paper_fill_allowed_by_route"])

        check_codes = {check["code"] for check in reason["checks"]}
        self.assertIn("route_not_paper_testable", check_codes)

    def test_proxy_short_is_allowed_with_reduced_allocation_and_proxy_metadata(self):
        candidate = _candidate(
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

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertEqual(guarded["paper_route_status"], "paper_testable_proxy")
        self.assertEqual(guarded["paper_route_type"], "proxy")
        self.assertTrue(guarded["paper_proxy_used"])
        self.assertTrue(guarded["paper_fill_allowed_by_route"])
        self.assertEqual(guarded["paper_allocation_multiplier"], 0.4)
        self.assertEqual(guarded["paper_proxy_route"]["route_id"], "perp_hedge_proxy")

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
