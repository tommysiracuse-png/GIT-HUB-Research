from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_order_router import apply_frontier_paper_guard  # noqa: E402
from execution_engine import build_order_ticket  # noqa: E402
from paper_route_registry import (  # noqa: E402
    apply_paper_route_registry,
    load_paper_route_registry,
)


class PaperRouteRegistryTests(unittest.TestCase):
    def test_registry_is_reloaded_as_read_only_data(self) -> None:
        first = load_paper_route_registry()
        self.assertTrue(first["paper_only"])
        self.assertTrue(first["routes"])

        first["routes"][0]["support_status"] = "supported"
        second = load_paper_route_registry()

        self.assertEqual("unsupported", second["routes"][0]["support_status"])
        self.assertFalse(second["routes"][0]["live_execution_allowed"])

    def test_high_failure_spot_short_is_tagged_for_non_blocking_paper_diagnostics(self) -> None:
        candidate = apply_paper_route_registry(
            {
                "venue": "OKX_SPOT",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "score": 80.0,
            }
        )

        self.assertEqual("unsupported", candidate["paper_route_registry_status"])
        self.assertEqual("diagnose", candidate["paper_route_registry"]["action"])
        self.assertEqual(80.0, candidate["score"])
        self.assertFalse(candidate.get("paper_entry_blocked", False))
        self.assertTrue(candidate.get("promotion_eligible", True))
        self.assertIn("spot_borrow", candidate["paper_route_required_permissions"])
        self.assertIsNone(candidate["paper_route_estimated_cost_bps"]["borrow"])
        self.assertFalse(candidate["paper_route_estimated_cost_bps"]["complete"])

    def test_unregistered_in_scope_route_is_penalized_idempotently(self) -> None:
        candidate = {
            "venue": "UNMAPPED_VENUE",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "short_frontier_spot",
            "score": 75.0,
        }

        first = apply_paper_route_registry(candidate)
        second = apply_paper_route_registry(first)

        self.assertEqual("unspecified", first["paper_route_registry_status"])
        self.assertEqual("penalize", first["paper_route_registry"]["action"])
        self.assertEqual(15.0, first["score"])
        self.assertEqual(15.0, second["score"])
        self.assertEqual(0.2, second["paper_allocation_multiplier"])
        self.assertFalse(second.get("paper_entry_blocked", False))
        self.assertEqual(
            ["crypto_spot", "margin_spot", "spot_short", "spot_borrow"],
            second["paper_route_required_permissions"],
        )

    def test_explicit_candidate_capabilities_are_not_treated_as_unspecified(self) -> None:
        candidate = apply_paper_route_registry(
            {
                "venue": "PAPER_TEST_VENUE",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "score": 75.0,
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertEqual("unspecified", candidate["paper_route_registry_status"])
        self.assertTrue(candidate["paper_route_registry"]["candidate_route_evidence_present"])
        self.assertEqual("tag", candidate["paper_route_registry"]["action"])
        self.assertEqual(75.0, candidate["score"])

    def test_matched_perpetual_routes_carry_permissions_and_fallback_costs(self) -> None:
        whitebit = apply_paper_route_registry(
            {
                "venue": "WHITEBIT",
                "trade_type": "perp_funding_basis",
                "direction": "funding_capture_short_perp",
                "score": 70.0,
            }
        )
        okx = apply_paper_route_registry(
            {
                "venue": "OKX",
                "trade_type": "perp_funding_basis",
                "direction": "funding_capture_long_perp",
                "score": 70.0,
                "estimated_round_trip_cost_bps": 9.5,
            }
        )

        self.assertEqual("conditional", whitebit["paper_route_registry_status"])
        self.assertEqual(["crypto_derivatives"], whitebit["paper_route_required_permissions"])
        self.assertEqual(16.0, whitebit["estimated_round_trip_cost_bps"])
        self.assertTrue(whitebit["paper_route_cost_fallback_applied"])
        self.assertEqual("supported", okx["paper_route_registry_status"])
        self.assertEqual(9.5, okx["estimated_round_trip_cost_bps"])
        self.assertNotIn("paper_route_cost_fallback_applied", okx)

    def test_prefill_guard_blocks_unsupported_tuple_and_live_mode_never_enables_it(self) -> None:
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "last": 50000.0,
            "trade_type": "perp_funding_basis",
            "direction": "long_perp_short_spot",
            "score": 80.0,
        }

        guarded = apply_frontier_paper_guard(candidate)
        live_tagged = apply_paper_route_registry(candidate, {"mode": "live"})

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertEqual("unsupported", guarded["paper_route_registry_status"])
        self.assertEqual("observe_only", live_tagged["paper_route_registry"]["action"])
        self.assertFalse(live_tagged["paper_route_registry"]["live_execution_allowed"])

    def test_unspecified_route_penalty_reduces_direct_paper_ticket_allocation(self) -> None:
        candidate = apply_paper_route_registry(
            {
                "venue": "UNMAPPED_VENUE",
                "inst_id": "ABC-USDT",
                "last": 10.0,
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "score": 75.0,
            }
        )
        ticket = build_order_ticket(
            candidate,
            {"paper_allocation_multiplier": 0.8},
            {"mode": "paper", "risk": {"paper_notional_usd": 1000.0}},
        )

        self.assertEqual(200.0, ticket["notional_usd"])
        self.assertEqual(200.0, ticket["legs"][0]["notional_usd"])


if __name__ == "__main__":
    unittest.main()
