from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_review import review_candidate  # noqa: E402
from execution_engine import build_order_ticket, execute_order  # noqa: E402
from route_resolver import enrich_candidates  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402
import strategy_reliability  # noqa: E402
from strategy_reliability import apply_strategy_reliability  # noqa: E402


def proxy_candidate() -> dict:
    return {
        "venue": "OKX",
        "inst_id": "OKX:BTC-USDT-SWAP",
        "market_key": "crypto_okx_btcusdt_swap_basis_paper",
        "signal_key": "okx_reverse_basis_signal",
        "trade_type": "perp_funding_basis",
        "direction": "long_perp_short_spot",
        "asset_class": "crypto_derivatives",
        "score": 80.0,
        "last": 50_000.0,
        "funding_bps": 15.0,
        "expected_funding_bps": 15.0,
        "basis_bps": 100.0,
        "edge_bps_estimate": 50.0,
        "gross_edge_bps_estimate": 100.0,
        "estimated_round_trip_cost_bps": 2.0,
        "liquidity_score": 0.95,
        "spread_bps": 1.0,
        "change_24h_pct": 1.0,
        "data_status": "reachable",
        "freshness_age_seconds": 1.0,
        "quality_score": 90.0,
        "quality_status": "verified",
    }


class ProxyRouteActivationTests(unittest.TestCase):
    def test_batch_enrichment_replaces_blocked_attempt_with_one_labeled_proxy(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)

        candidates = enrich_candidates([proxy_candidate()], settings)

        self.assertEqual(1, len(candidates))
        candidate = candidates[0]
        self.assertTrue(candidate["proxy_replaces_direct_candidate"])
        self.assertTrue(candidate["paper_proxy_activated"])
        self.assertEqual("conditional_crypto_route_paper", candidate["execution_route"]["route_id"])
        self.assertEqual(["spot_borrow"], candidate["execution_route"]["missing_permissions"])
        self.assertEqual("okx_derivatives_paper", candidate["paper_proxy_route"]["route_id"])
        self.assertEqual("proxy_not_live_equivalent", candidate["paper_execution_semantics"])
        self.assertTrue(candidate["paper_proxy_not_live_equivalent"])
        self.assertEqual(0.25, candidate["paper_allocation_multiplier"])
        self.assertGreater(candidate["score"], 80.0)
        self.assertEqual(candidate["score"], candidate["proxy_quality_score"])
        self.assertEqual("proxy_not_live_equivalent", candidate["paper_proxy_counterfactual"]["execution_semantics"])
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertFalse(candidate["paper_route_eligibility"]["suppressed"])
        self.assertTrue(candidate["signal_key"].startswith("PAPER_PROXY|okx_derivatives_paper|"))
        self.assertEqual("okx_reverse_basis_signal", candidate["direct_source_signal_key"])

    def test_active_pipeline_rejects_legacy_one_leg_paired_proxy(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        candidate = enrich_candidates([proxy_candidate()], settings)[0]
        with tempfile.TemporaryDirectory() as tmp:
            old_json = strategy_reliability.REPORT_JSON
            old_md = strategy_reliability.REPORT_MD
            strategy_reliability.REPORT_JSON = pathlib.Path(tmp) / "strategy_reliability.json"
            strategy_reliability.REPORT_MD = pathlib.Path(tmp) / "strategy_reliability.md"
            try:
                candidates, _ = apply_strategy_reliability([candidate], settings)
            finally:
                strategy_reliability.REPORT_JSON = old_json
                strategy_reliability.REPORT_MD = old_md
        candidate = candidates[0]
        review = review_candidate(candidate, settings, {})

        self.assertEqual("approve_conditional_paper_trade", review["decision"])
        self.assertEqual("okx_derivatives_paper", review["effective_route_id"])
        self.assertTrue(review["proxy_not_live_equivalent"])
        self.assertEqual([], review["hard_blocks"])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        execution = execute_order(conn, candidate, review, settings)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual([], execution["fills"])
        order = execution["order"]
        self.assertEqual("okx_derivatives_paper", order["route_id"])
        self.assertEqual("proxy_not_live_equivalent", order["execution_semantics"])
        self.assertTrue(order["proxy_not_live_equivalent"])
        self.assertTrue(order["paper_proxy_not_live_equivalent"])
        self.assertEqual("paper_proxy", order["signal_stats_scope"])
        self.assertEqual(0.0, order["notional_usd"])
        self.assertEqual(
            "blocked_paired_direct_requires_bounded_queue",
            order["status"],
        )
        self.assertEqual("invalid_or_incomplete", order["paired_direct_contract_status"])

    def test_proxy_activation_is_paper_only_and_requires_only_borrow_to_be_missing(self) -> None:
        live_settings = copy.deepcopy(DEFAULT_SETTINGS)
        live_settings["mode"] = "live"
        live = enrich_candidates([proxy_candidate()], live_settings)[0]
        self.assertFalse(live.get("paper_proxy_activated", False))

        missing_spot_settings = copy.deepcopy(DEFAULT_SETTINGS)
        missing_spot_settings["account_capabilities"]["crypto_spot"] = False
        blocked = enrich_candidates([proxy_candidate()], missing_spot_settings)[0]
        self.assertFalse(blocked.get("paper_proxy_activated", False))
        self.assertGreater(blocked["score"], 0.0)
        self.assertLess(blocked["score"], 80.0)
        self.assertFalse(blocked.get("paper_entry_blocked", False))

    def test_incomplete_proxy_metadata_fails_closed_before_order_emission(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        candidate = enrich_candidates([proxy_candidate()], settings)[0]
        review = review_candidate(candidate, settings, {})
        candidate["paper_proxy_not_live_equivalent"] = False

        order = build_order_ticket(candidate, review, settings)

        self.assertEqual("blocked_invalid_paper_proxy_metadata", order["status"])
        self.assertFalse(order["proxy_not_live_equivalent"])


if __name__ == "__main__":
    unittest.main()
