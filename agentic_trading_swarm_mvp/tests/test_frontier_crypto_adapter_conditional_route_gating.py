import unittest

from src.frontier_crypto_adapter import (
    paper_only_validate_recommendation,
    paper_only_validate_recommendation_destination,
)


class TestConditionalCryptoRouteGating(unittest.TestCase):
    def test_conditional_crypto_route_missing_fields_blocks_publication(self):
        recommendation = {
            "market_key": "CRYPTO_OKX_CONDITIONAL_ROUTE_GATING",
            "asset_class": "crypto",
            "conditional": True,
            "signal": "basis_trade",
            "signal_direction": "long_perp_short_spot",
            "confidence": 0.81,
            "base_asset": "BTC",
            "quote_asset": "USDT",
        }

        result = paper_only_validate_recommendation_destination(
            recommendation,
            execution_destination="paper_engine",
        )

        reviewed = result["recommendation"]
        self.assertFalse(result["destination_valid"])
        self.assertEqual(reviewed["action"], "no_op")
        self.assertEqual(reviewed["paper_only_warning"], "conditional_route_incomplete_or_inconsistent")
        self.assertTrue(reviewed["route_publication_blocked"])
        self.assertIn("route_primary_venue", reviewed["paper_route_review"]["missing_route_fields"])

    def test_conditional_crypto_route_valid_configuration_is_allowed(self):
        recommendation = {
            "market_key": "CRYPTO_OKX_CONDITIONAL_ROUTE_GATING",
            "asset_class": "crypto",
            "conditional": True,
            "signal": "basis_trade",
            "signal_direction": "long_perp_short_spot",
            "confidence": 0.81,
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "route_primary_venue": "OKX",
            "route_primary_symbol": "BTC-USDT-SWAP",
            "route_primary_instrument_type": "perpetual",
            "route_primary_side": "buy",
            "route_hedge_venue": "OKX_SPOT",
            "route_hedge_symbol": "BTC-USDT",
            "route_hedge_instrument_type": "spot",
            "route_hedge_side": "sell",
            "route_inventory_mode": "pre_borrowed_inventory",
            "route_confidence": 0.82,
        }

        result = paper_only_validate_recommendation_destination(
            recommendation,
            execution_destination="paper_engine",
        )

        reviewed = result["recommendation"]
        self.assertTrue(result["destination_valid"])
        self.assertIsNone(reviewed.get("paper_only_warning"))
        self.assertTrue(reviewed["paper_route_review"]["approved"])
        self.assertNotIn("action", reviewed)

    def test_unknown_base_or_quote_asset_forces_zero_route_confidence_and_rejects(self):
        recommendation = {
            "market_key": "CRYPTO_OKX_CONDITIONAL_ROUTE_GATING",
            "asset_class": "crypto",
            "conditional": True,
            "signal": "basis_trade",
            "signal_direction": "short_perp_long_spot",
            "confidence": 0.74,
            "base_asset": "",
            "quote_asset": "USDT",
            "route_primary_venue": "OKX",
            "route_primary_symbol": "BTC-USDT-SWAP",
            "route_primary_instrument_type": "perpetual",
            "route_primary_side": "sell",
            "route_hedge_venue": "OKX_SPOT",
            "route_hedge_symbol": "BTC-USDT",
            "route_hedge_instrument_type": "spot",
            "route_hedge_side": "buy",
            "route_inventory_mode": "cash_and_carry",
            "route_confidence": 0.61,
        }

        result = paper_only_validate_recommendation_destination(
            recommendation,
            execution_destination="paper_engine",
        )

        reviewed = result["recommendation"]
        self.assertEqual(reviewed["route_confidence"], 0.0)
        self.assertEqual(reviewed["action"], "no_op")
        self.assertIn(
            "unknown_base_or_quote_asset",
            reviewed["paper_route_review"]["inconsistent_route_fields"],
        )

        validation = paper_only_validate_recommendation(
            recommendation,
            execution_destination="paper_engine",
        )
        self.assertTrue(validation["rejected"])
        self.assertTrue(validation["route_publication_blocked"])


if __name__ == "__main__":
    unittest.main()
