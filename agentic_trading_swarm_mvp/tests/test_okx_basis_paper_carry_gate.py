import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_order_router import frontier_shadow_filter_reason
from strategy_reliability import okx_basis_paper_carry_gate_record


class OkxBasisPaperCarryGateTests(unittest.TestCase):
    def test_blocks_rich_basis_when_net_funding_is_not_positive_after_costs(self):
        candidate = {
            "market_key": "crypto_okx_btcusdt_swap_basis_paper",
            "signal_key": "okx_basis_long_carry",
            "expected_funding_bps": 2.0,
            "estimated_fee_bps": 1.5,
            "estimated_slippage_bps": 0.7,
            "basis_bps": 35.0,
        }

        record = okx_basis_paper_carry_gate_record(candidate)

        self.assertIsNotNone(record)
        self.assertFalse(record["eligible"])
        self.assertEqual(record["guard"], "paper_okx_basis_carry_gate")
        self.assertAlmostEqual(record["net_funding_bps"], -0.2, places=6)
        self.assertIn(
            "non_positive_net_funding_expectation",
            {item["code"] for item in record["failed_checks"]},
        )

    def test_allows_positive_net_carry_for_okx_basis_paper_setup(self):
        candidate = {
            "market_key": "crypto_okx_btcusdt_swap_basis_paper",
            "signal_key": "okx_basis_long_carry",
            "expected_funding_bps": 4.0,
            "estimated_fee_bps": 1.0,
            "estimated_slippage_bps": 0.5,
            "basis_bps": 22.0,
        }

        record = okx_basis_paper_carry_gate_record(candidate)

        self.assertIsNotNone(record)
        self.assertTrue(record["eligible"])
        self.assertEqual(record["suppression_action"], "allow")
        self.assertAlmostEqual(record["net_funding_bps"], 2.5, places=6)

    def test_router_surfaces_hold_no_trade_reason_for_weak_carry_basis_state(self):
        candidate = {
            "market_key": "crypto_okx_btcusdt_swap_basis_paper",
            "signal_key": "okx_basis_long_carry",
            "variant": "elevated_basis_weak_funding",
            "expected_funding_bps": 1.4,
            "estimated_fee_bps": 0.8,
            "estimated_slippage_bps": 0.5,
            "basis_bps": 28.0,
        }

        reason = frontier_shadow_filter_reason(candidate)

        self.assertIsNotNone(reason)
        self.assertEqual(reason["guard"], "paper_okx_basis_carry_gate")
        self.assertFalse(reason["paper_fill_allowed"])
        self.assertEqual(reason["conviction_cap"], "hold")
        self.assertIn(
            "weak_carry_for_rich_basis",
            {item["code"] for item in reason["failed_checks"]},
        )

    def test_non_okx_basis_candidate_is_ignored(self):
        candidate = {"market_key": "equity_us_momentum_paper", "signal_key": "simple_breakout"}

        self.assertIsNone(okx_basis_paper_carry_gate_record(candidate))


if __name__ == "__main__":
    unittest.main()
