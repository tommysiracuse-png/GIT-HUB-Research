import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_reliability import (
    apply_paper_route_lineage_confirmation,
    paper_context_promotion_guard_record,
    paper_route_lineage_record,
)


class PaperContextPromotionGuardRecordTests(unittest.TestCase):
    def test_returns_none_without_source_context(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
        }

        self.assertIsNone(paper_context_promotion_guard_record(candidate))

    def test_unconfirmed_translation_is_observation_only_with_score_haircut(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "coinbase",
                "direction": "short",
                "trade_family": "basis_carry",
            },
        }

        record = paper_context_promotion_guard_record(candidate)

        self.assertIsNotNone(record)
        self.assertFalse(record["eligible"])
        self.assertTrue(record["promotion_blocked"])
        self.assertEqual(record["reason"], "route_local_confirmation_missing")
        self.assertEqual(set(record["mismatched_fields"]), {"venue", "direction", "trade_family"})
        self.assertEqual(record["paper_score_multiplier"], 0.15)
        self.assertTrue(record["paper_fill_allowed"])
        self.assertTrue(record["observation_only"])

    def test_allows_exact_context_match(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "kraken",
                "direction": "long",
                "trade_family": "frontier_spot_alpha",
            },
        }

        record = paper_context_promotion_guard_record(candidate)

        self.assertIsNotNone(record)
        self.assertTrue(record["eligible"])
        self.assertFalse(record["promotion_blocked"])
        self.assertEqual(set(record["matching_fields"]), {"venue", "direction", "trade_family"})
        self.assertEqual(record["paper_score_multiplier"], 1.0)

    def test_explicit_compatibility_rule_still_requires_local_confirmation(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "coinbase",
                "direction": "long",
                "trade_family": "frontier_spot_alpha",
            },
            "compatibility_rule": {
                "allow_cross_context": True,
                "fields": ["venue"],
                "rule_id": "shared_usd_liquidity_pool",
            },
        }

        record = paper_context_promotion_guard_record(candidate)

        self.assertFalse(record["eligible"])
        self.assertTrue(record["compatibility_rule_logged"])
        self.assertEqual(record["compatibility_rule"]["rule_id"], "shared_usd_liquidity_pool")

    def test_same_market_price_and_liquidity_confirmation_releases_translation(self):
        candidate = {
            "score": 100.0,
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "coinbase",
                "direction": "long",
                "trade_family": "frontier_spot_alpha",
            },
            "route_local_confirmation": {
                "native_price_action_confirmed": True,
                "native_liquidity_confirmed": True,
            },
        }

        record = paper_context_promotion_guard_record(candidate)
        adjustment = apply_paper_route_lineage_confirmation(candidate)

        self.assertTrue(record["eligible"])
        self.assertFalse(record["observation_only"])
        self.assertIsNone(adjustment)
        self.assertEqual(100.0, candidate["score"])
        self.assertFalse(candidate["paper_route_lineage"]["observation_only"])

    def test_native_candidate_receives_lineage_tag(self):
        record = paper_route_lineage_record(
            {"venue": "kraken", "direction": "long", "signal_family": "frontier_spot_alpha"}
        )

        self.assertEqual("native", record["lineage_type"])
        self.assertEqual("not_required", record["confirmation_status"])


if __name__ == "__main__":
    unittest.main()
