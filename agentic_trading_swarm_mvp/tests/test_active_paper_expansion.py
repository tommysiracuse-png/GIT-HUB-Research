import copy
import datetime as dt
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as frontier
import frontier_data_quality
import global_market_discovery_scanner as global_scanner
from okx_perp_scanner import instrument_asset_context
from okx_signal_research import _basis_regime_ok
from settings import DEFAULT_SETTINGS
from strategy_reliability import paper_family_quarantine_record


def chart(symbol: str) -> dict:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if symbol == "SPY":
        closes = [110.0 - index * 0.2 for index in range(30)]
    elif symbol == "UUP":
        closes = [100.0 - index * 0.02 for index in range(30)]
    else:
        closes = [100.0 + index * 0.3 for index in range(30)]
    return {
        "meta": {
            "symbol": symbol,
            "exchangeName": "TEST",
            "currency": "USD",
            "currentTradingPeriod": {"regular": {"start": now - 3600, "end": now + 3600}},
        },
        "timestamp": [now - (29 - index) * 900 for index in range(30)],
        "indicators": {"quote": [{"close": closes, "volume": [1_000_000] * 30}]},
    }


class ActivePaperExpansionTests(unittest.TestCase):
    def test_requested_cohort_is_reserved(self):
        cfg = copy.deepcopy(DEFAULT_SETTINGS)
        targets = global_scanner._build_targets([], cfg)
        symbols = {proxy.get("symbol") for _, proxy in targets if proxy.get("active_paper_cohort")}
        self.assertEqual(global_scanner.ACTIVE_PAPER_SYMBOLS, symbols)

    def test_volatility_candidate_uses_spy_shock_confirmation(self):
        original = global_scanner.fetch_chart
        try:
            global_scanner.fetch_chart = chart
            global_scanner._CHART_CACHE.clear()
            discovery = {"venue_or_source": "Cboe Global Markets", "priority": 95, "confidence": 0.8}
            proxy = {
                "symbol": "VIXY",
                "label": "VIXY",
                "surface": "volatility_long",
                "cohort_surface": "volatility_long",
                "active_paper_cohort": True,
            }
            candidate = global_scanner._build_proxy_candidate(discovery, proxy, DEFAULT_SETTINGS)
        finally:
            global_scanner.fetch_chart = original
            global_scanner._CHART_CACHE.clear()
        self.assertEqual("long_proxy", candidate["direction"])
        self.assertEqual("long_vol_spy_shock_confirmation_v1", candidate["strategy_variant"])
        self.assertEqual("verified_proxy", candidate["proxy_quality_status"])
        self.assertIsNone(paper_family_quarantine_record(candidate))

    def test_bitkub_v3_list_and_depth_symbols(self):
        target = {
            "venue": "BITKUB",
            "market_type": "spot",
            "route_id": "bitkub_spot_public",
            "region": "Southeast Asia",
            "url": "https://api.bitkub.com/api/v3/market/ticker",
        }
        result = {
            "payload": [
                {
                    "symbol": "BTC_THB",
                    "last": "2100000",
                    "highest_bid": "2099000",
                    "lowest_ask": "2101000",
                    "base_volume": "2",
                    "quote_volume": "4200000",
                }
            ],
            "data_status": "reachable",
            "http_status": "200",
            "latency_ms": 25,
        }
        rows = frontier._parse_bitkub_ticker(target, result)
        self.assertEqual(1, len(rows))
        self.assertEqual("BTC", rows[0]["base"])
        self.assertEqual("THB", rows[0]["quote"])
        self.assertEqual("BTC_THB", frontier_data_quality._format_symbol("BITKUB", "BTC_THB"))

    def test_okx_context_is_asset_specific(self):
        context = instrument_asset_context(
            "BTC-USDT-SWAP",
            {"instFamily": "BTC-USDT", "settleCcy": "USDT"},
        )
        self.assertEqual("BTC", context["base_asset"])
        self.assertEqual("BTC", context["base"])
        self.assertEqual("USDT", context["quote"])
        self.assertEqual("USDT", context["settlement_currency"])
        self.assertEqual("perpetual_swap", context["instrument_type"])
        self.assertEqual("okx_perpetual_swap", context["market_surface"])
        self.assertEqual("BTC-USDT", context["index_id"])
        self.assertEqual("asset_specific", context["basis_context_status"])
        candidate = {
            **context,
            "basis_persistence_status": "same_asset_persistent",
            "basis_momentum_cooling": True,
            "basis_bps": 50.0,
            "funding_bps": 1.0,
            "change_24h_pct": 2.0,
            "direction": "basis_mean_reversion_short_perp",
        }
        self.assertTrue(_basis_regime_ok(candidate))
        candidate["basis_context_status"] = "unresolved"
        self.assertFalse(_basis_regime_ok(candidate))


if __name__ == "__main__":
    unittest.main()
