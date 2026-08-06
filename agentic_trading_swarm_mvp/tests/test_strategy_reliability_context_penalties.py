import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import strategy_reliability as sr


class StrategyReliabilityContextPenaltyTests(unittest.TestCase):
    def test_context_priors_promote_standard_liquid_carry_route(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "OKX",
            "asset_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard"},
        }

        detail = sr.apply_paper_context_priors(candidate, {"mode": "paper", "allow_live_trading": False})

        self.assertEqual(detail["base_signal_score"], 60.0)
        self.assertEqual(detail["feasibility_prior"], 6.0)
        self.assertEqual(detail["context_slice_prior"], 8.0)
        self.assertEqual(detail["context_slice_key"], "OKX_SPOT|long|standard")
        self.assertEqual(detail["liquidity_prior"], 4.0)
        self.assertEqual(detail["strategy_family"], "carry_or_funding_capture")
        self.assertEqual(candidate["final_paper_score"], 75.0)
        self.assertEqual(candidate["score"], 75.0)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_hard_gated")
        self.assertFalse(candidate["promotion_eligible"])
        self.assertFalse(candidate["paper_context_top_rank_eligible"])
        self.assertNotIn("paper_entry_blocked", candidate)

    def test_context_priors_use_positive_realized_slice_evidence(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "OKX",
            "asset_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard"},
        }

        detail = sr.apply_paper_context_priors(
            candidate,
            {
                "mode": "paper",
                "allow_live_trading": False,
                "paper_context_priors": {
                    "realized_context_stats": {
                        "OKX_SPOT|long|standard": {
                            "closed_count": 10,
                            "avg_pnl_bps": 20.0,
                            "win_rate": 0.7,
                        }
                    }
                },
            },
        )

        self.assertEqual(detail["realized_context_key"], "OKX_SPOT|long|standard")
        self.assertEqual(detail["realized_context_closed_count"], 10)
        self.assertEqual(detail["realized_context_avg_pnl_bps"], 20.0)
        self.assertEqual(detail["realized_context_prior"], 4.0)
        self.assertEqual(candidate["final_paper_score"], 75.0)
        self.assertEqual(candidate["score"], 75.0)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_hard_gated")
        self.assertFalse(candidate["paper_context_top_rank_eligible"])

    def test_context_priors_allow_proven_positive_standard_slice_to_clear_top_rank_cap(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "OKX",
            "asset_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard"},
        }

        detail = sr.apply_paper_context_priors(
            candidate,
            {
                "mode": "paper",
                "allow_live_trading": False,
                "paper_context_priors": {
                    "realized_context_stats": {
                        "OKX_SPOT|long|standard": {
                            "closed_count": 30,
                            "avg_pnl_bps": 20.0,
                            "win_rate": 0.7,
                        }
                    }
                },
            },
        )

        self.assertEqual(detail["realized_context_closed_count"], 30)
        self.assertEqual(candidate["final_paper_score"], 82.0)
        self.assertEqual(candidate["score"], 82.0)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_not_blocked")
        self.assertTrue(candidate["paper_context_top_rank_eligible"])

    def test_context_priors_differentiate_standard_and_conditional_slices(self) -> None:
        standard = {
            "score": 60.0,
            "venue": "BYBIT_SPOT",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard"},
        }
        conditional = {
            **standard,
            "execution_feasibility": {"status": "conditional"},
        }

        standard_detail = sr.apply_paper_context_priors(standard, {"mode": "paper", "allow_live_trading": False})
        conditional_detail = sr.apply_paper_context_priors(
            conditional,
            {"mode": "paper", "allow_live_trading": False},
        )

        self.assertEqual(standard_detail["context_slice_key"], "BYBIT_SPOT|long|standard")
        self.assertEqual(conditional_detail["context_slice_key"], "BYBIT_SPOT|long|conditional")
        self.assertEqual(standard_detail["context_slice_prior"], 6.0)
        self.assertEqual(conditional_detail["context_slice_prior"], -3.0)
        self.assertGreater(standard["score"], conditional["score"])

    def test_context_priors_rank_down_weak_conditional_convergence_without_blocking(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "MEXC",
            "direction": "long_frontier_spot",
            "trade_type": "basis_mean_reversion",
            "liquidity_score": 0.3,
            "execution_feasibility": {"status": "conditional"},
        }

        detail = sr.apply_paper_context_priors(candidate, {"mode": "paper", "allow_live_trading": False})

        self.assertEqual(detail["raw_total_prior"], -33.0)
        self.assertEqual(candidate["score"], 27.0)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_promotion_gated")
        self.assertFalse(candidate["promotion_eligible"])
        self.assertNotIn("paper_entry_blocked", candidate)
        self.assertNotIn("paper_fill_allowed", candidate)

    def test_context_priors_penalize_negative_conditional_slice_more_strongly(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "MEXC",
            "direction": "long_frontier_spot",
            "trade_type": "basis_mean_reversion",
            "liquidity_score": 0.3,
            "execution_feasibility": {"status": "conditional"},
        }

        detail = sr.apply_paper_context_priors(
            candidate,
            {
                "mode": "paper",
                "allow_live_trading": False,
                "paper_context_priors": {
                    "realized_context_stats": {
                        "MEXC|long|conditional": {
                            "closed_count": 12,
                            "avg_pnl_bps": -20.0,
                            "win_rate": 0.33,
                        }
                    }
                },
            },
        )

        self.assertEqual(detail["realized_context_key"], "MEXC|long|conditional")
        self.assertTrue(detail["realized_context_persistent_negative"])
        self.assertEqual(detail["realized_context_prior"], -15.75)
        self.assertEqual(detail["raw_total_prior"], -48.75)
        self.assertEqual(candidate["score"], 11.25)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_promotion_gated")
        self.assertFalse(candidate["promotion_eligible"])
        self.assertNotIn("paper_entry_blocked", candidate)

    def test_exceptional_signal_keeps_negative_context_as_diagnostic(self) -> None:
        candidate = {
            "score": 90.0,
            "venue": "MEXC",
            "direction": "long_frontier_spot",
            "trade_type": "basis_mean_reversion",
            "liquidity_score": 0.3,
            "execution_feasibility": {"status": "conditional"},
        }

        detail = sr.apply_paper_context_priors(candidate, {"mode": "paper", "allow_live_trading": False})

        self.assertTrue(detail["exceptional_signal_override"])
        self.assertEqual(detail["raw_total_prior"], -33.0)
        self.assertEqual(detail["total_prior"], 0.0)
        self.assertTrue(detail["exceptional_signal_override"])
        self.assertEqual(candidate["score"], 35.0)
        self.assertEqual(candidate["paper_context_prior_status"], "ranked_hard_gated")

    def test_context_priors_are_inert_for_live_configuration(self) -> None:
        candidate = {
            "score": 60.0,
            "venue": "OKX",
            "asset_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "spot_carry",
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "standard"},
        }

        detail = sr.apply_paper_context_priors(candidate, {"mode": "live", "allow_live_trading": False})

        self.assertIsNone(detail)
        self.assertEqual(candidate["score"], 60.0)
        self.assertNotIn("paper_context_prior", candidate)

    def test_context_penalties_disabled_by_default(self) -> None:
        candidate = {
            "score": 50.0,
            "symbol": "BTC/USD",
            "trade_type": "paper_cross_market_radar",
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["baseline"])

        self.assertEqual(candidate["score"], 50.0)
        self.assertNotIn("paper_score_context_penalty", candidate)
        self.assertNotIn("paper_score_context_penalty", reliability)

    def test_context_penalties_reduce_score_and_log_components(self) -> None:
        candidate = {
            "score": 100.0,
            "symbol": "BTC/USD",
            "trade_type": "paper_cross_market_radar",
            "paper_only_context_penalties": True,
            "supporting_market_data_age_seconds": 120.0,
            "supporting_market_refresh_window_seconds": 60.0,
            "realized_volatility": 0.08,
            "volatility_stress_threshold": 0.04,
            "correlated_markets_confirmed": False,
            "market_breadth_confirmed": False,
            "independent_feature_count": 1,
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["penalized"])
        detail = candidate["paper_score_context_penalty"]

        self.assertAlmostEqual(detail["base_score"], 100.0, places=3)
        self.assertAlmostEqual(detail["total_multiplier"], 0.522006, places=6)
        self.assertAlmostEqual(candidate["score"], 52.201, places=3)
        self.assertEqual(detail["dominant_penalty_reason"], "freshness_penalty")
        self.assertEqual(detail["terms"]["freshness_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["regime_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["confirmation_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["concentration_penalty"]["multiplier"], 0.85)
        self.assertEqual(reliability["paper_score_context_penalty"]["final_score"], 52.201)
        self.assertIn("paper_context_penalty:freshness_penalty", candidate["risk_notes"])

    def test_context_penalties_are_neutral_when_inputs_are_missing(self) -> None:
        candidate = {
            "score": 80.0,
            "symbol": "ETH/USD",
            "trade_type": "paper_cross_market_radar",
            "paper_only_context_penalties": True,
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["neutral"])
        detail = candidate["paper_score_context_penalty"]

        self.assertEqual(candidate["score"], 80.0)
        self.assertEqual(detail["total_multiplier"], 1.0)
        self.assertIsNone(detail["dominant_penalty_reason"])
        self.assertFalse(detail["applied"])
        self.assertEqual(reliability["paper_score_context_penalty"]["base_score"], 80.0)
        self.assertNotIn("paper_context_penalty:freshness_penalty", candidate.get("risk_notes", []))


if __name__ == "__main__":
    unittest.main()
