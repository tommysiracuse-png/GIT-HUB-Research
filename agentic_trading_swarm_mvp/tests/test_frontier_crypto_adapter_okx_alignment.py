import unittest

try:
    from src.frontier_crypto_adapter import _paper_only_route_requirements_packet
except ImportError:  # pragma: no cover
    from frontier_crypto_adapter import _paper_only_route_requirements_packet


class TestPaperOnlyOkxCarryAlignmentGate(unittest.TestCase):
    def test_blocks_when_basis_and_funding_disagree(self):
        route_status = {
            "venue": "OKX",
            "market_key": "okx.perp_funding_basis",
            "strategy_family": "perp_funding_basis",
            "direction": "short",
            "route_requirement_status": "supported",
            "basis_support": "supported",
            "perp_support": "supported",
            "api_surface": "public_market_data",
            "fee_reference": "public_taker_fee_schedule",
            "basis_bps": -15.0,
            "expected_funding_capture_bps": 8.0,
        }

        packet = _paper_only_route_requirements_packet(route_status, {})

        self.assertTrue(packet["paper_only_route_blocked"])
        self.assertEqual(packet["paper_only_block_reason"], "carry_misaligned")
        self.assertFalse(packet["route_complete"])
        self.assertEqual(packet["route_actionability"], "low_priority_research")
        self.assertIn("carry_alignment", packet["critical_missing_fields"])

    def test_blocks_when_trade_direction_fights_aligned_carry(self):
        route_status = {
            "venue": "OKX",
            "market_key": "okx.perp_funding_basis",
            "strategy_family": "perp_funding_basis",
            "direction": "long",
            "route_requirement_status": "supported",
            "basis_support": "supported",
            "perp_support": "supported",
            "api_surface": "public_market_data",
            "fee_reference": "public_taker_fee_schedule",
            "basis_bps": 14.0,
            "expected_funding_capture_bps": 6.0,
        }

        packet = _paper_only_route_requirements_packet(route_status, {})

        self.assertTrue(packet["paper_only_route_blocked"])
        self.assertEqual(packet["paper_only_block_reason"], "direction_fights_carry")
        self.assertEqual(packet["carry_alignment_review"]["expected_direction"], "short")

    def test_allows_when_direction_matches_aligned_carry(self):
        route_status = {
            "venue": "OKX",
            "market_key": "okx.perp_funding_basis",
            "strategy_family": "perp_funding_basis",
            "direction": "short",
            "route_requirement_status": "supported",
            "basis_support": "supported",
            "perp_support": "supported",
            "api_surface": "public_market_data",
            "fee_reference": "public_taker_fee_schedule",
            "basis_bps": 12.0,
            "expected_funding_capture_bps": 5.0,
        }

        packet = _paper_only_route_requirements_packet(route_status, {})

        self.assertFalse(packet["paper_only_route_blocked"])
        self.assertTrue(packet["route_complete"])
        self.assertEqual(packet["route_actionability"], "actionable_paper")
        self.assertEqual(packet["carry_alignment_review"]["reason"], "carry_aligned")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
