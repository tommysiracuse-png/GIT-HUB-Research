import datetime as dt
import unittest

from src.frontier_data_quality import paper_only_route_quality_record


class PaperOnlyCrossAssetRegimeWatchTests(unittest.TestCase):
    def test_cross_asset_regime_watch_triggers_monitoring_flag_without_blocking(self):
        observed_at = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
        as_of = observed_at + dt.timedelta(seconds=5)
        result = paper_only_route_quality_record(
            best_bid=100.0,
            best_ask=100.1,
            bid_size=5.0,
            ask_size=5.0,
            observed_at=observed_at,
            as_of=as_of,
            route_status="eligible",
            config={
                "paper_us_cross_asset_risk_regime": {
                    "enabled": True,
                    "signals": {
                        "equity_momentum_weakened": True,
                        "front_end_yields_rising": True,
                        "bitcoin_underperforming_equities": True,
                        "crypto_confirms_risk_appetite": False,
                    },
                }
            },
        )

        watch = result["cross_asset_regime_watch"]
        self.assertTrue(watch["enabled"])
        self.assertTrue(watch["triggered"])
        self.assertEqual(watch["priority"], "high")
        self.assertEqual(watch["aligned_signal_count"], 3)
        self.assertIn("cross_asset_risk_off_divergence_watch", result["monitoring_flags"])
        self.assertFalse(result["paper_ineligible"])
        self.assertEqual(result["paper_decision"], "eligible")

    def test_cross_asset_regime_watch_requires_equity_confirmation(self):
        result = paper_only_route_quality_record(
            best_bid=100.0,
            best_ask=100.1,
            bid_size=5.0,
            ask_size=5.0,
            route_status="eligible",
            config={
                "paper_us_cross_asset_risk_regime": {
                    "enabled": True,
                    "signals": {
                        "front_end_yields_rising": True,
                        "bitcoin_underperforming_equities": True,
                    },
                }
            },
        )

        watch = result["cross_asset_regime_watch"]
        self.assertFalse(watch["triggered"])
        self.assertEqual(result["monitoring_flags"], [])


if __name__ == "__main__":
    unittest.main()
