import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signal_safety import _cfg, _classify_signal, _policy_for


class SignalKeyProbationPolicyTests(unittest.TestCase):
    def test_feature_flag_defaults_off(self) -> None:
        cfg = _cfg({})
        stats = {
            "signal_key": "paper.mean_revert.alpha",
            "closed_count": 12,
            "avg_pnl_bps": -20.0,
            "win_rate": 0.45,
            "updated_at": "2026-07-18T00:00:00+00:00",
        }
        recent = {
            "closed_count": 12,
            "avg_pnl_bps": -10.0,
            "win_rate": 0.45,
            "worst_bps": -40.0,
        }

        mode, reason = _classify_signal(stats, recent, cfg)

        self.assertEqual(mode, "healthy")
        self.assertEqual(reason, "no_governor_action")

    def test_enabled_downweight_marks_probation_with_ttl_metadata(self) -> None:
        cfg = _cfg(
            {
                "signal_safety": {
                    "signal_key_probation_enabled": True,
                    "signal_key_probation_min_closed_trades": 12,
                    "signal_key_probation_expectancy_bps": -15.0,
                    "signal_key_probation_mode": "downweight",
                    "signal_key_probation_weight": 0.25,
                    "signal_key_probation_ttl_hours": 72,
                }
            }
        )
        settings = {"risk": {"min_net_edge_bps": 2.0}, "scanner": {"min_base_score": 35.0}}
        stats = {
            "signal_key": "paper.breakout.lossy_key",
            "closed_count": 14,
            "avg_pnl_bps": -18.0,
            "win_rate": 0.42,
            "updated_at": "2026-07-18T00:00:00+00:00",
        }
        recent = {
            "closed_count": 8,
            "avg_pnl_bps": -12.0,
            "win_rate": 0.38,
            "worst_bps": -55.0,
        }

        mode, reason = _classify_signal(stats, recent, cfg)
        policy = _policy_for(mode, reason, stats, recent, settings, cfg)

        self.assertEqual(mode, "probation")
        self.assertEqual(reason, "signal_key_probation_expectancy")
        self.assertFalse(policy["pause_entries"])
        self.assertEqual(policy["allocation_multiplier"], 0.25)
        self.assertEqual(policy["policy_scope"], "paper_only")
        self.assertTrue(policy["paper_only"])
        self.assertIsNotNone(policy["expires_at"])
        self.assertEqual(policy["trigger_closed_trades"], 14)
        self.assertEqual(policy["trigger_expectancy_bps"], -18.0)

    def test_block_mode_uses_quarantine(self) -> None:
        cfg = _cfg(
            {
                "signal_safety": {
                    "signal_key_probation_enabled": True,
                    "signal_key_probation_expectancy_bps": -15.0,
                    "signal_key_probation_mode": "block",
                }
            }
        )
        stats = {"signal_key": "paper.momentum.lossy_key", "closed_count": 12, "avg_pnl_bps": -25.0, "win_rate": 0.40}
        recent = {"closed_count": 5, "avg_pnl_bps": -10.0, "win_rate": 0.40, "worst_bps": -50.0}

        mode, reason = _classify_signal(stats, recent, cfg)

        self.assertEqual(mode, "quarantine")
        self.assertEqual(reason, "signal_key_probation_expectancy")


if __name__ == "__main__":
    unittest.main()
