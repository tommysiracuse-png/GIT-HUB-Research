import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as frontier_crypto_adapter  # noqa: E402


class FrontierCryptoAdapterPolicyTests(unittest.TestCase):
    def test_default_policy_exposes_disabled_yahoo_proxy_shadow_evaluation(self) -> None:
        policy = frontier_crypto_adapter._paper_trade_policy_from_loaded_registry()
        shadow = policy["shadow_evaluation"]
        self.assertFalse(shadow["enabled"])
        self.assertEqual(["YAHOO_PROXY|global_proxy_momentum"], shadow["target_market_keys"])
        self.assertEqual(90, shadow["freshness_gate_seconds"])
        self.assertEqual(15, shadow["session_boundary_block_minutes"])
        self.assertEqual("freshness_and_session_gate", shadow["candidate_mode"])

    def test_loaded_policy_deep_merges_shadow_overrides(self) -> None:
        loaded = {
            "paper_trade_policy": {
                "shadow_evaluation": {
                    "enabled": True,
                    "freshness_gate_seconds": 45,
                    "log_fields": ["proxy_age_seconds", "suppressed_reason"],
                }
            }
        }
        policy = frontier_crypto_adapter._paper_trade_policy_from_loaded_registry(loaded)
        shadow = policy["shadow_evaluation"]
        self.assertTrue(shadow["enabled"])
        self.assertEqual(45, shadow["freshness_gate_seconds"])
        self.assertEqual(15, shadow["session_boundary_block_minutes"])
        self.assertEqual("paper_baseline", shadow["control_mode"])
        self.assertEqual(["proxy_age_seconds", "suppressed_reason"], shadow["log_fields"])

    def test_single_market_key_override_is_normalized_to_a_list(self) -> None:
        loaded = {
            "paper_trade_policy": {
                "shadow_evaluation": {"target_market_keys": "YAHOO_PROXY|global_proxy_momentum"}
            }
        }
        policy = frontier_crypto_adapter._paper_trade_policy_from_loaded_registry(loaded)
        self.assertEqual(
            ["YAHOO_PROXY|global_proxy_momentum"],
            policy["shadow_evaluation"]["target_market_keys"],
        )
import unittest

from src.frontier_crypto_adapter import DEFAULT_PAPER_TRADE_POLICY, DEFAULT_REGISTRY


class FrontierCryptoAdapterPolicyTests(unittest.TestCase):
    def test_default_paper_trade_policy_is_paper_only_and_conservative(self) -> None:
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["mode"], "paper_only")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["execution"], "simulated")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["min_confirmation_score"], 0.70)
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["divergence_block"], "enabled")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["high_volatility_posture"], "monitor_first")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["single_asset_override"], "disabled")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["state_if_unconfirmed"], "flat")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["state_if_divergent"], "monitor")

    def test_default_registry_enables_paper_trade_policy(self) -> None:
        filters = DEFAULT_REGISTRY["filters"]
        self.assertTrue(filters["paper_trade_policy_enabled"])
        self.assertGreaterEqual(filters["min_cross_venue_count"], 2)
        self.assertEqual(DEFAULT_REGISTRY["paper_trade_policy"]["mode"], "paper_only")


if __name__ == "__main__":
    unittest.main()
