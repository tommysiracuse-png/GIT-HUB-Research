import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from paper_order_router import apply_frontier_paper_guard


def _candidate(**overrides):
    candidate = {
        "market_surface": "frontier_crypto_venue_map",
        "signal_family": "frontier_crypto_venue_map",
        "direction": "short_frontier_spot",
        "trade_type": "conditional",
        "symbol": "DOGE/USDT",
        "venue": "GATE",
        "score": 71.0,
    }
    candidate.update(overrides)
    return candidate


class FrontierConditionalShortAdmissionTests(unittest.TestCase):
    def test_direct_executable_short_remains_admissible_and_annotated(self):
        guarded = apply_frontier_paper_guard(
            _candidate(
                borrow_confirmed=True,
                paper_short_simulation_allowed=True,
                borrowable=True,
                borrow_cost_bps=4.0,
                margin_eligible=True,
                venue_capabilities={"paper_route_feasible": True},
                execution_feasibility={"status": "executable"},
            )
        )

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertEqual(guarded["paper_route_status"], "executable")
        self.assertEqual(guarded["paper_route_type"], "direct")
        self.assertTrue(guarded["paper_fill_allowed_by_route"])
        self.assertEqual(guarded["paper_allocation_multiplier"], 1.0)

    def test_route_feasibility_guard_can_be_disabled_for_rollback(self):
        candidate = _candidate(
            execution_feasibility={"status": "research_only"},
        )

        guarded = apply_frontier_paper_guard(
            candidate,
            {"frontier_route_feasibility_guard_enabled": False},
        )

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertNotIn("frontier_route_feasibility", guarded)

    def test_research_only_short_is_filtered_by_default(self):
        guarded = apply_frontier_paper_guard(
            _candidate(execution_feasibility={"status": "research_only"})
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual(guarded["paper_route_status"], "research_only")


if __name__ == "__main__":
    unittest.main()
