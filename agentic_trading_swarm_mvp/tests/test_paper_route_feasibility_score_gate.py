from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import execute_order  # noqa: E402
from paper_order_router import (  # noqa: E402
    apply_frontier_paper_guard,
    paper_route_feasibility_gate_review,
)
from route_resolver import enrich_candidate_with_route  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


def route_sensitive_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "venue": "OKX",
        "inst_id": "BTC-USDT-SWAP",
        "direction": "long_perp_short_spot",
        "trade_type": "perp_spot_basis",
        "signal_key": "OKX|perp_spot_basis|long_perp_short_spot|conditional",
        "route_status": "conditional",
        "route_sensitive": True,
        "route_feasibility_score": 0.64,
        "last": 50000.0,
        "score": 80.0,
    }
    candidate.update(overrides)
    return candidate


class PaperRouteFeasibilityScoreGateTests(unittest.TestCase):
    def test_route_sensitive_conditional_candidate_below_threshold_is_blocked(self) -> None:
        guarded = apply_frontier_paper_guard(route_sensitive_candidate())

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertEqual(
            "paper_route_feasibility_score_gate",
            guarded["candidate_reject_detail"]["guard"],
        )
        self.assertEqual(0.65, guarded["paper_route_feasibility_gate"]["threshold"])

    def test_threshold_is_inclusive_and_configurable(self) -> None:
        candidate = route_sensitive_candidate(route_feasibility_score=0.65)
        review = paper_route_feasibility_gate_review(candidate)
        stricter = paper_route_feasibility_gate_review(
            candidate,
            {"paper_route_feasibility_gate": {"min_score": 0.7}},
        )

        self.assertTrue(review["eligible"])
        self.assertEqual("admit", review["action"])
        self.assertFalse(stricter["eligible"])

    def test_missing_score_fails_closed_for_marked_candidate(self) -> None:
        candidate = route_sensitive_candidate()
        candidate.pop("route_feasibility_score")

        review = paper_route_feasibility_gate_review(candidate)

        self.assertTrue(review["applies"])
        self.assertFalse(review["eligible"])
        self.assertEqual("route_feasibility_score_missing", review["reason"])

    def test_gate_is_paper_only_and_has_rollback_toggle(self) -> None:
        candidate = route_sensitive_candidate(route_feasibility_score=0.1)

        live = paper_route_feasibility_gate_review(candidate, {"mode": "live"})
        disabled = paper_route_feasibility_gate_review(
            candidate,
            {"paper_route_feasibility_gate": {"enabled": False}},
        )

        self.assertFalse(live["applies"])
        self.assertTrue(live["eligible"])
        self.assertFalse(disabled["applies"])
        self.assertTrue(disabled["eligible"])

    def test_standard_or_non_route_sensitive_candidates_are_unchanged(self) -> None:
        standard = paper_route_feasibility_gate_review(
            route_sensitive_candidate(route_status="standard", route_feasibility_score=0.1)
        )
        ordinary_conditional = paper_route_feasibility_gate_review(
            route_sensitive_candidate(route_sensitive=False, route_feasibility_score=0.1)
        )

        self.assertFalse(standard["applies"])
        self.assertFalse(ordinary_conditional["applies"])

    def test_scope_includes_cross_venue_basis_and_venue_prerequisites(self) -> None:
        cross_venue = paper_route_feasibility_gate_review(
            {
                "route_sensitive": True,
                "route_status": "conditional",
                "trade_type": "cross_venue_basis",
                "direction": "long_cheap_venue_sell_rich_venue",
                "route_feasibility_score": 0.4,
            }
        )
        venue_api = paper_route_feasibility_gate_review(
            {
                "route_sensitive": True,
                "route_status": "conditional",
                "trade_type": "venue_specific_event",
                "route_feasibility_score": 0.4,
                "missing_requirements": ["venue_api_access", "margin_permission"],
            }
        )
        unrelated = paper_route_feasibility_gate_review(
            {
                "route_sensitive": True,
                "route_status": "conditional",
                "trade_type": "long_cash_equity",
                "route_feasibility_score": 0.4,
            }
        )

        self.assertIn("cross_venue_basis", cross_venue["scope_reasons"])
        self.assertTrue(cross_venue["applies"])
        self.assertIn("venue_api_or_margin_prerequisite", venue_api["scope_reasons"])
        self.assertTrue(venue_api["applies"])
        self.assertFalse(unrelated["applies"])

    def test_nested_route_requirements_mark_and_scope_venue_prerequisite(self) -> None:
        review = paper_route_feasibility_gate_review(
            {
                "route_status": "conditional",
                "trade_type": "venue_specific_event",
                "route_feasibility_score": 0.4,
                "route_requirements": {
                    "route_sensitivity_reasons": ["venue_api_or_margin_prerequisite"],
                    "missing_prerequisites": ["venue_api_access"],
                },
            }
        )

        self.assertTrue(review["route_sensitive"])
        self.assertIn("venue_api_or_margin_prerequisite", review["scope_reasons"])
        self.assertTrue(review["applies"])
        self.assertFalse(review["eligible"])

    def test_resolver_marks_and_scores_conditional_short_spot_route(self) -> None:
        enriched = enrich_candidate_with_route(
            {
                "venue": "OKX",
                "inst_id": "BTC-USDT-SWAP",
                "asset_class": "crypto_derivatives",
                "direction": "long_perp_short_spot",
                "trade_type": "perp_funding_basis",
                "score": 80.0,
            },
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        self.assertTrue(enriched["route_sensitive"])
        self.assertIn("short_spot", enriched["route_sensitivity_reasons"])
        self.assertLess(enriched["route_feasibility_score"], 0.65)
        self.assertEqual(
            enriched["route_feasibility_score"],
            enriched["execution_feasibility"]["route_feasibility_score"],
        )

    def test_execution_boundary_uses_synthetic_research_when_route_score_is_low(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        review = {
            "decision": "approve_conditional_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 20.0,
            "feasibility_status": "conditional",
            "route_status": "conditional",
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(
            conn,
            route_sensitive_candidate(
                route_feasibility_score=0.5,
                explicit_synthetic_proxy=True,
            ),
            review,
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        self.assertTrue(result["paper_filled"])
        self.assertEqual(1, len(result["fills"]))
        self.assertEqual("paper_filled", result["order"]["status"])
        self.assertEqual("synthetic_research_paper", result["order"]["route_id"])
        row = conn.execute("select status from execution_orders").fetchone()
        self.assertEqual("paper_filled", row["status"])

    def test_enriched_conditional_short_stays_diagnostic_only_at_execution_boundary(self) -> None:
        candidate = enrich_candidate_with_route(
            {
                "venue": "OKX",
                "inst_id": "BTC-USDT-SWAP",
                "asset_class": "crypto_derivatives",
                "direction": "long_perp_short_spot",
                "trade_type": "perp_funding_basis",
                "signal_key": "OKX|perp_funding_basis|long_perp_short_spot|conditional",
                "last": 50000.0,
                "score": 80.0,
            },
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        guarded = apply_frontier_paper_guard(candidate, copy.deepcopy(DEFAULT_SETTINGS))

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertFalse(guarded.get("paper_entry_blocked", False))
        self.assertTrue(guarded["paper_route_feasibility_gate"]["applies"])
        self.assertTrue(guarded["paper_route_feasibility_gate"]["eligible"])
        self.assertEqual(
            "diagnostic_only",
            guarded["paper_route_feasibility_gate"]["action"],
        )


if __name__ == "__main__":
    unittest.main()
