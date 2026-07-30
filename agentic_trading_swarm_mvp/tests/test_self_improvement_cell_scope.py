import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from strategy_reliability import evaluate_paper_cell_policy, paper_signal_cell, paper_signal_cell_key


class TestSelfImprovementCellScope(unittest.TestCase):
    def test_signal_cell_key_distinguishes_sibling_cells(self) -> None:
        base = {
            "signal_family": "frontier_crypto_venue_map",
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_crypto_venue_map_alpha",
            "variant": "v2",
            "venue": "GATE",
            "paper_route_status": "paper_testable_proxy",
        }
        long_cell = paper_signal_cell({**base, "direction": "long"})
        short_cell = paper_signal_cell({**base, "direction": "short"})

        self.assertNotEqual(long_cell["cell_key"], short_cell["cell_key"])
        self.assertEqual(long_cell["venue"], "GATE")
        self.assertEqual(long_cell["direction"], "long")
        self.assertEqual(long_cell["paper_route_status"], "paper_testable_proxy")

    def test_mixed_cells_are_decided_independently(self) -> None:
        promoted = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "OKX_SPOT",
                "direction": "long",
                "paper_route_status": "executable",
                "closed_count": 6,
                "avg_pnl_bps": 8.25,
                "win_rate": 0.67,
            }
        )
        reverted = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "OKX_SPOT",
                "direction": "short",
                "paper_route_status": "blocked",
                "closed_count": 6,
                "avg_pnl_bps": -8.75,
                "win_rate": 0.33,
            }
        )

        self.assertEqual(promoted["decision"], "promoted")
        self.assertEqual(promoted["action"], "promote_cell")
        self.assertEqual(reverted["decision"], "reverted")
        self.assertEqual(reverted["action"], "rollback_cell")
        self.assertNotEqual(promoted["cell_key"], reverted["cell_key"])

    def test_probation_expiry_can_revert_a_losing_cell(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "signal_key": "frontier_crypto_venue_map|alpha",
                "strategy": "frontier_alpha",
                "variant": "v2",
                "venue": "GATE",
                "direction": "short",
                "paper_route_status": "paper_testable_proxy",
                "closed_count": 2,
                "avg_pnl_bps": -1.5,
                "prior_state": "probation",
                "probation_started_at": "2026-01-01T00:00:00+00:00",
            },
            config={"paper_cell_policy": {"min_closed_trades": 3, "probation_ttl_days": 7}},
            now="2026-01-10T00:00:00+00:00",
        )

        self.assertEqual(result["decision"], "reverted")
        self.assertEqual(result["action"], "rollback_cell")
        self.assertTrue(result["probation_expired"])

    def test_legacy_record_without_explicit_cell_fields_gets_safe_fallback_scope(self) -> None:
        result = evaluate_paper_cell_policy(
            {
                "strategy": "legacy_strategy",
                "variant": "legacy_variant",
                "closed_count": 4,
                "avg_pnl_bps": 1.25,
            }
        )

        self.assertEqual(result["decision"], "promoted")
        self.assertTrue(result["cell_key"])
        self.assertEqual(result["cell"]["scope"], "paper_signal_cell_v1")
        self.assertEqual(result["cell"]["strategy"], "legacy_strategy")

    def test_public_key_helper_matches_cell_record(self) -> None:
        candidate = {
            "signal_key": "frontier_crypto_venue_map|alpha",
            "strategy": "frontier_alpha",
            "variant": "v2",
            "venue": "GATE",
            "direction": "long",
            "paper_route_status": "executable",
        }
        self.assertEqual(paper_signal_cell_key(candidate), paper_signal_cell(candidate)["cell_key"])


if __name__ == "__main__":
    unittest.main()
