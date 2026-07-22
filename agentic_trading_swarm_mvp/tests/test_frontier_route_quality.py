import datetime as dt
import unittest

try:
    from src.frontier_crypto_adapter import paper_only_valr_observation_from_public_payloads
    from src.frontier_data_quality import paper_only_route_quality_record
except ImportError:  # pragma: no cover - direct module fallback
    from frontier_crypto_adapter import paper_only_valr_observation_from_public_payloads
    from frontier_data_quality import paper_only_route_quality_record


class PaperOnlyRouteQualityRecordTests(unittest.TestCase):
    def test_blocks_stale_wide_and_unavailable_route(self):
        observed_at = dt.datetime(2026, 7, 22, 0, 0, tzinfo=dt.timezone.utc)
        evaluated_at = observed_at + dt.timedelta(seconds=30)

        quality = paper_only_route_quality_record(
            best_bid=100.0,
            best_ask=101.0,
            bid_size=0.25,
            ask_size=0.25,
            observed_at=observed_at,
            as_of=evaluated_at,
            intended_paper_notional_usd=500.0,
            venue_spread_baseline_bps=20.0,
            route_status="maintenance",
        )

        self.assertTrue(quality["paper_ineligible"])
        self.assertEqual("blocked", quality["paper_decision"])
        self.assertEqual("blocked", quality["simulated_slippage_tier"])
        self.assertIn("route_unavailable", quality["blocking_reasons"])
        self.assertIn("stale_quote", quality["blocking_reasons"])
        self.assertIn("insufficient_top_of_book_depth", quality["blocking_reasons"])
        self.assertIn("spread_above_baseline", quality["blocking_reasons"])

    def test_degrades_thin_but_reachable_route(self):
        quality = paper_only_route_quality_record(
            best_bid=100.0,
            best_ask=100.10,
            bid_size=2.0,
            ask_size=2.0,
            observed_at="2026-07-22T00:00:00+00:00",
            as_of="2026-07-22T00:00:05+00:00",
            intended_paper_notional_usd=180.0,
            venue_spread_baseline_bps=8.0,
            route_status="degraded",
        )

        self.assertFalse(quality["paper_ineligible"])
        self.assertEqual("degraded", quality["paper_decision"])
        self.assertEqual("elevated", quality["simulated_slippage_tier"])
        self.assertLess(quality["simulated_size_factor"], 1.0)
        self.assertIn("route_status_marginal", quality["warnings"])


class PaperOnlyValrObservationRouteQualityTests(unittest.TestCase):
    def test_observation_attaches_route_quality_fields(self):
        ticker_payload = {
            "bidPrice": "100.0",
            "askPrice": "100.2",
            "lastTradedPrice": "100.1",
            "quoteVolume": "2500000",
            "baseVolume": "25000",
        }
        orderbook_payload = {
            "bids": [{"price": "100.0", "quantity": "4.0"}],
            "asks": [{"price": "100.2", "quantity": "4.5"}],
        }
        observation = paper_only_valr_observation_from_public_payloads(
            "BTC/ZAR",
            ticker_payload=ticker_payload,
            orderbook_payload=orderbook_payload,
            trades_payload=[],
            quote_timestamp="2026-07-22T00:00:00+00:00",
            evaluation_timestamp="2026-07-22T00:00:03+00:00",
            route_status="available",
            intended_paper_notional_usd=200.0,
            venue_spread_baseline_bps=25.0,
        )

        self.assertIsInstance(observation["route_quality"], dict)
        self.assertFalse(observation["paper_ineligible"])
        self.assertEqual("normal", observation["simulated_slippage_tier"])
        self.assertEqual("eligible", observation["route_quality"]["paper_decision"])
        self.assertGreater(observation["route_quality"]["depth_to_size_ratio"], 1.0)
        self.assertIsNone(observation["paper_ineligible_reason"])

    def test_observation_preserves_legacy_as_of_behavior(self):
        observation = paper_only_valr_observation_from_public_payloads("BTCZAR", as_of="2026-07-22T00:00:00+00:00")
        self.assertEqual("2026-07-22T00:00:00+00:00", observation["observed_at"])
        self.assertFalse(observation["paper_ineligible"])


if __name__ == "__main__":
    unittest.main()
