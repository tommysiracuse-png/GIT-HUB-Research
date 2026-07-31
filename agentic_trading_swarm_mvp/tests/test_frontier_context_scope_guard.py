import unittest

from src.frontier_crypto_adapter import _paper_only_context_scoping_review
from src.frontier_data_quality import _paper_only_strategy_scope_review


class PaperOnlyStrategyScopeGuardTests(unittest.TestCase):
    def test_rejects_strategy_lab_candidate_missing_required_scope_fields(self):
        review = _paper_only_strategy_scope_review(
            {
                "lab_id": "lab-1",
                "venue": "OKX",
                "instrument_family": "spot",
                "direction": "short",
                "target_surface": "short_frontier_spot",
            }
        )
        self.assertTrue(review["applies"])
        self.assertTrue(review["rejected_by_scope"])
        self.assertEqual(review["reason"], "missing_scope_fields")
        self.assertEqual(
            set(review["missing_scope_fields"]),
            {"venue", "instrument_family", "direction", "surface"},
        )

    def test_rejects_strategy_lab_scope_mismatch(self):
        review = _paper_only_strategy_scope_review(
            {
                "lab_id": "lab-2",
                "allowed_venue": "OKX",
                "allowed_instrument_family": "spot",
                "allowed_direction": "short",
                "allowed_surface": "short_frontier_spot",
                "venue": "OKX",
                "instrument_family": "spot",
                "direction": "short",
                "target_surface": "long_frontier_spot",
            }
        )
        self.assertTrue(review["rejected_by_scope"])
        self.assertEqual(review["reason"], "scope_mismatch")
        self.assertEqual([item["field"] for item in review["mismatches"]], ["surface"])

    def test_adapter_context_scoping_blocks_scope_mismatch(self):
        allowed_record = {
            "lab_id": "lab-3",
            "allowed_venue": "OKX",
            "allowed_instrument_family": "spot",
            "allowed_direction": "short",
            "allowed_surface": "short_frontier_spot",
            "venue": "OKX",
            "instrument_family": "spot",
            "direction": "short",
        }
        result = _paper_only_context_scoping_review(
            allowed_record,
            origin_surface="short_frontier_spot",
            target_surface="long_frontier_spot",
            same_surface=False,
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
