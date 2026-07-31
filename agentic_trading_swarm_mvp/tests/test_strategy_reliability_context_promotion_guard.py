import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from strategy_reliability import paper_context_promotion_guard_record


class PaperContextPromotionGuardRecordTests(unittest.TestCase):
    def test_returns_none_without_source_context(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
        }

        self.assertIsNone(paper_context_promotion_guard_record(candidate))

    def test_blocks_mismatched_context_without_explicit_rule(self):
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
        self.assertEqual(record["reason"], "paper_context_promotion_mismatch")
        self.assertEqual(set(record["mismatched_fields"]), {"venue", "direction", "trade_family"})
        self.assertEqual(record["paper_score_multiplier"], 0.0)

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

    def test_allows_mismatch_when_explicit_rule_covers_it(self):
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

        self.assertTrue(record["eligible"])
        self.assertTrue(record["compatibility_rule_logged"])
        self.assertEqual(record["compatibility_rule"]["rule_id"], "shared_usd_liquidity_pool")


if __name__ == "__main__":
    unittest.main()
