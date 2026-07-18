import datetime as dt
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signal_safety import _cfg, _classify_signal


class SignalSafetyFreshnessGuardTests(unittest.TestCase):
    def test_opportunistic_signal_stales_after_horizon(self) -> None:
        stats = {
            "signal_key": "proxy_momentum|opportunistic_breakout",
            "closed_count": 1,
            "avg_pnl_bps": 24.0,
            "win_rate": 1.0,
            "updated_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(),
        }
        recent = {
            "closed_count": 0,
            "avg_pnl_bps": None,
            "win_rate": None,
            "worst_bps": None,
        }
        cfg = _cfg({"signal_safety": {"stale_signal_horizon_seconds": 60}})

        mode, reason = _classify_signal(stats, recent, cfg)

        self.assertEqual(mode, "quarantine")
        self.assertEqual(reason, "stale_signal_decayed")

    def test_exploit_signal_decays_when_recent_average_falls_below_threshold(self) -> None:
        stats = {
            "signal_key": "exploit|proxy_momentum_reversal",
            "closed_count": 12,
            "avg_pnl_bps": 19.0,
            "win_rate": 0.67,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        recent = {
            "closed_count": 6,
            "avg_pnl_bps": 3.0,
            "win_rate": 0.50,
            "worst_bps": -18.0,
        }
        cfg = _cfg({"signal_safety": {"stale_signal_min_recent_avg_bps": 5.0}})

        mode, reason = _classify_signal(stats, recent, cfg)

        self.assertEqual(mode, "quarantine")
        self.assertEqual(reason, "stale_signal_decayed")

    def test_non_sensitive_signal_keeps_existing_observe_behavior(self) -> None:
        stats = {
            "signal_key": "mean_reversion|core",
            "closed_count": 1,
            "avg_pnl_bps": 12.0,
            "win_rate": 1.0,
            "updated_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat(),
        }
        recent = {"closed_count": 0, "avg_pnl_bps": None, "win_rate": None, "worst_bps": None}
        cfg = _cfg({"signal_safety": {"stale_signal_horizon_seconds": 60}})

        mode, reason = _classify_signal(stats, recent, cfg)

        self.assertEqual(mode, "observe")
        self.assertEqual(reason, "not_enough_closed_trades")


if __name__ == "__main__":
    unittest.main()
