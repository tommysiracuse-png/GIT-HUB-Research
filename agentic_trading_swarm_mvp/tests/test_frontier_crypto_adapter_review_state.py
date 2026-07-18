import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from frontier_crypto_adapter import (  # noqa: E402
    paper_only_conditional_short_route_requirements,
    paper_only_cross_market_review_state,
    validate_paper_recommendation_payload,
)


class FrontierCryptoAdapterReviewStateTests(unittest.TestCase):
    def test_review_state_defaults_to_observe_only_when_evidence_is_incomplete(self):
        result = paper_only_cross_market_review_state(
            evidence={
                "data_quality": "good",
                "execution_scope": "paper-only",
            },
            signal_state="uncertain",
        )

        self.assertEqual(result["paper_review_state"], "observe_only")
        self.assertEqual(result["portfolio_action"], "no position change")
        self.assertEqual(result["sizing"], "0 simulated allocation change")
        self.assertEqual(result["signal_state"], "uncertain")
        self.assertEqual(result["missing_evidence_fields"], ["risk_view", "signal_state"])

    def test_validate_payload_falls_back_to_hold_when_confidence_is_too_low(self):
        result = validate_paper_recommendation_payload(
            payload={
                "action": "propose_code_change",
                "evidence": {"paper_only_scope": "public data only"},
                "market_key": "VALR|frontier_crypto_venue_map",
                "priority": 82,
                "proposed_change": "Add VALR observation support.",
                "rationale": "Improve regional price discovery.",
                "title": "VALR spot adapter",
                "confidence": 0.42,
            },
            confidence_threshold=0.65,
        )

        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["market_key"], "VALR|frontier_crypto_venue_map")
        self.assertEqual(result["evidence"]["issue_type"], "insufficient_recommendation_evidence")
        self.assertEqual(result["evidence"]["confidence"], 0.42)
        self.assertEqual(result["evidence"]["confidence_threshold"], 0.65)
        self.assertIn("Paper-trading only", result["evidence"]["paper_scope"])

    def test_valr_spot_short_requirements_are_explicitly_unsupported(self):
        result = paper_only_conditional_short_route_requirements(venue="VALR_SPOT")

        self.assertEqual(result["venue"], "VALR_SPOT")
        self.assertEqual(result["venue_key"], "VALR")
        self.assertFalse(result["supports_spot_short"])
        self.assertEqual(result["support_status"], "unsupported")
        self.assertIsNone(result["requires_margin_permission"])
        self.assertIsNone(result["requires_borrow_check"])
        self.assertEqual(result["margin_mode_hint"], "unsupported")
        self.assertEqual(result["api_route_hint"], "unsupported")
        self.assertIn("spot_short_unsupported", result["notes"])

    def test_unknown_venues_remain_unknown_without_explicit_registry_entries(self):
        result = paper_only_conditional_short_route_requirements(venue="UNKNOWN_SPOT")

        self.assertEqual(result["support_status"], "unknown")
        self.assertIn("support_unknown", result["notes"])


if __name__ == "__main__":
    unittest.main()
import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_review_state


class TestPaperOnlyCrossMarketReviewState(unittest.TestCase):
    def test_incomplete_evidence_defaults_to_observe_only(self):
        result = paper_only_cross_market_review_state(
            evidence={
                "data_quality": "previous response invalid or incomplete",
                "execution_scope": "paper only",
            },
            signal_state="inconclusive",
        )

        self.assertEqual(result["paper_review_state"], "observe_only")
        self.assertEqual(result["portfolio_action"], "no position change")
        self.assertEqual(result["sizing"], "0 simulated allocation change")
        self.assertIn("risk_view", result["missing_evidence_fields"])

    def test_complete_evidence_can_pass_review(self):
        result = paper_only_cross_market_review_state(
            evidence={
                "data_quality": "complete",
                "execution_scope": "paper only",
                "risk_view": "elevated model uncertainty",
                "signal_state": "validated",
            },
            signal_state="validated",
        )

        self.assertEqual(result["paper_review_state"], "review_ok")
        self.assertEqual(result["portfolio_action"], "paper candidate eligible")
        self.assertEqual(result["sizing"], "paper-sized allocation permitted")


if __name__ == "__main__":
    unittest.main()
