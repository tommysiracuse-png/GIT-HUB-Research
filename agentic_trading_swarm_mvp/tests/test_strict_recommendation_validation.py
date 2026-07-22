import unittest

from src.code_evolution import (
    _PAPER_ONLY_RECOMMENDATION_FALLBACK,
    _finalize_strict_recommendation_object,
)


class StrictRecommendationValidationTests(unittest.TestCase):
    def test_finalizer_keeps_valid_paper_only_object(self):
        candidate = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Safe paper-only recommendation",
            "rationale": "Valid structured output for downstream handling.",
            "market_key": "paper.cross_market",
            "evidence": {"constraint": "paper_only", "issue": "schema"},
            "proposed_change": {"goal": "validate output", "constraints": "paper_only"},
        }

        finalized = _finalize_strict_recommendation_object(candidate)

        self.assertEqual(finalized["market_key"], "paper.cross_market")
        self.assertEqual(finalized["title"], candidate["title"])

    def test_finalizer_falls_back_on_extra_top_level_field(self):
        candidate = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Invalid with extra key",
            "rationale": "Should be rejected.",
            "market_key": "paper.cross_market",
            "evidence": {"constraint": "paper_only", "issue": "schema"},
            "proposed_change": {"goal": "validate output", "constraints": "paper_only"},
            "unexpected": True,
        }

        finalized = _finalize_strict_recommendation_object(candidate)

        self.assertEqual(finalized, _PAPER_ONLY_RECOMMENDATION_FALLBACK)

    def test_finalizer_falls_back_on_non_paper_market_key(self):
        candidate = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Normalize market key",
            "rationale": "Market key must stay paper scoped.",
            "market_key": "cross_market",
            "evidence": {"constraint": "paper_only", "issue": "schema"},
            "proposed_change": {"goal": "validate output", "constraints": "paper_only"},
        }

        finalized = _finalize_strict_recommendation_object(candidate)

        self.assertEqual(finalized, _PAPER_ONLY_RECOMMENDATION_FALLBACK)
