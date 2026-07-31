import unittest

from src.frontier_crypto_adapter import (
    _PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG,
    _paper_only_exact_surface_scope_review,
)


class PaperOnlyExactSurfaceScopeReviewTests(unittest.TestCase):
    def test_accepts_matching_strategy_lab_scope_contract(self):
        route_status = {
            "market_key": "STRATEGY_LAB",
            "paper_policy_flags": [_PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG],
            "source_venue_scope": "OKX",
            "source_instrument_scope": "perpetual",
            "source_trade_family_scope": "funding_capture",
            "source_direction_scope": "receive_funding",
            "source_execution_scope": "spot_plus_perp",
        }
        profile = {
            "venue": "okx",
            "instrument_scope": "perp",
            "strategy_family": "carry",
            "direction": "short",
            "required_side": "spot_plus_perp",
        }

        review = _paper_only_exact_surface_scope_review(route_status, profile)

        self.assertTrue(review["applies"])
        self.assertFalse(review["blocked"])
        self.assertEqual(review["reason"], "scope_matched")
        self.assertEqual(review["source_scope_contract"]["instrument_scope"], "perp")
        self.assertEqual(review["source_scope_contract"]["trade_family_scope"], "carry")
        self.assertEqual(review["target_scope_contract"]["trade_family_scope"], "carry")

    def test_blocks_missing_required_scope_fields_for_strategy_lab(self):
        route_status = {
            "market_key": "STRATEGY_LAB",
            "paper_policy_flags": [_PAPER_ONLY_STRATEGY_LAB_EXACT_CONTEXT_PROMOTION_FLAG],
        }
        profile = {
            "venue": "okx",
            "instrument_scope": "spot",
            "strategy_family": "momentum",
            "direction": "long",
            "required_side": "spot",
        }

        review = _paper_only_exact_surface_scope_review(route_status, profile)

        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "missing_scope_fields")
        self.assertCountEqual(
            review["missing_fields"],
            [
                "source_venue_scope",
                "source_instrument_scope",
                "source_trade_family_scope",
                "source_direction_scope",
                "source_execution_scope",
            ],
        )

    def test_blocks_cross_surface_mismatch(self):
        route_status = {
            "market_key": "STRATEGY_LAB",
            "source_venue_scope": "okx",
            "source_instrument_scope": "perp",
            "source_trade_family_scope": "carry",
            "source_direction_scope": "short",
            "source_execution_scope": "spot_plus_perp",
        }
        profile = {
            "venue": "okx",
            "instrument_scope": "perpetual",
            "strategy_family": "momentum",
            "direction": "sell",
            "required_side": "spot_plus_perp",
        }

        review = _paper_only_exact_surface_scope_review(route_status, profile)

        self.assertEqual(review["mismatched_fields"], ["trade_family_scope"])
