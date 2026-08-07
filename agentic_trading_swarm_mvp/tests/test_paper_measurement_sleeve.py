from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_measurement_sleeve import apply_bounded_measurement_probe  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


def settings() -> dict:
    return {
        "mode": "paper",
        "allow_live_trading": False,
        "paper_expansion": {
            "measurement_probe_enabled": True,
            "runtime_phase": "measurement",
            "discovery_retire_min_labels": 20,
            "measurement_probe_min_net_edge_bps": 2.0,
        },
    }


def candidate(**overrides) -> dict:
    value = {
        "venue": "GATE",
        "inst_id": "GATE:ABC_USDT",
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "last": 10.0,
        "quality_status": "verified",
        "execution_feasibility": {"status": "standard"},
        "signal_stats_scope": "direct",
        "shadow_filtered": True,
        "paper_fill_allowed": False,
        "paper_entry_blocked": True,
        "_paper_admission_queue_id": "queue-1",
        "_paper_admission_lane": "discovery",
        "_paper_admission_reliable_labels": 3,
        "_paper_measurement_probe_allowed": True,
        "_paper_measurement_probe_guard": "strategy_reliability",
    }
    value.update(overrides)
    return value


def review() -> dict:
    return {
        "decision": "approve_paper_trade",
        "hard_blocks": [],
        "net_edge_bps_estimate": 12.0,
    }


class PaperMeasurementSleeveTests(unittest.TestCase):
    def test_reopens_only_prequalified_under_sampled_direct_candidate(self) -> None:
        result = apply_bounded_measurement_probe(candidate(), review(), settings())

        self.assertTrue(result["paper_fill_allowed"])
        self.assertFalse(result["shadow_filtered"])
        self.assertEqual("direct", result["signal_stats_scope"])
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(1.0, result["paper_allocation_multiplier"])

    def test_never_overrides_quality_or_fill_gate(self) -> None:
        low_quality = apply_bounded_measurement_probe(
            candidate(quality_status="unknown"), review(), settings()
        )
        hard_fill_gate = apply_bounded_measurement_probe(
            candidate(paper_fill_gate_blocked=True), review(), settings()
        )

        self.assertFalse(low_quality["paper_fill_allowed"])
        self.assertFalse(hard_fill_gate["paper_fill_allowed"])

    def test_never_overrides_synthetic_or_route_blocked_candidate(self) -> None:
        synthetic = apply_bounded_measurement_probe(
            candidate(signal_stats_scope="synthetic_research"), review(), settings()
        )
        conditional = apply_bounded_measurement_probe(
            candidate(execution_feasibility={"status": "conditional"}), review(), settings()
        )

        self.assertFalse(synthetic["paper_fill_allowed"])
        self.assertFalse(conditional["paper_fill_allowed"])

    def test_stops_after_discovery_sample_limit(self) -> None:
        result = apply_bounded_measurement_probe(
            candidate(_paper_admission_reliable_labels=20), review(), settings()
        )

        self.assertFalse(result["paper_fill_allowed"])

    def test_live_or_unapproved_paths_fail_closed(self) -> None:
        live = settings()
        live["allow_live_trading"] = True
        rejected = review()
        rejected["decision"] = "reject"

        self.assertFalse(
            apply_bounded_measurement_probe(candidate(), review(), live)["paper_fill_allowed"]
        )
        self.assertFalse(
            apply_bounded_measurement_probe(candidate(), rejected, settings())["paper_fill_allowed"]
        )

    def test_execution_creates_direct_hundred_dollar_paper_fill_with_lineage(self) -> None:
        runtime = copy.deepcopy(DEFAULT_SETTINGS)
        runtime["mode"] = "paper"
        runtime["allow_live_trading"] = False
        runtime["paper_expansion"] = settings()["paper_expansion"]
        runtime["paper_exploration"]["enabled"] = False
        runtime["risk"]["paper_notional_usd"] = 100.0
        queued = candidate(
            admission_key="admission-1",
            episode_id="episode-1",
            score=80.0,
            edge_bps_estimate=20.0,
            gross_edge_bps_estimate=45.0,
            estimated_round_trip_cost_bps=20.0,
            spread_bps=2.0,
            liquidity_score=0.9,
            quality_action="normal",
            anomaly_flags=[],
            execution_route={
                "status": "standard",
                "route_status": "standard",
                "route_id": "frontier_crypto_spot_paper",
            },
        )
        approved = {
            **review(),
            "learned_score": 80.0,
            "confidence": 0.8,
            "gross_edge_bps": 45.0,
            "modeled_cost_bps": 20.0,
            "paper_allocation_multiplier": 1.0,
            "feasibility_status": "standard",
            "route_status": "standard",
            "route_id": "frontier_crypto_spot_paper",
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            result = execute_order(conn, queued, approved, runtime)
            saved = conn.execute(
                "select status,notional_usd,admission_key,admission_episode_id "
                "from execution_orders where id=?",
                (result["order_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["paper_filled"])
        self.assertEqual("paper_filled", saved["status"])
        self.assertEqual(100.0, saved["notional_usd"])
        self.assertEqual("admission-1", saved["admission_key"])
        self.assertEqual("episode-1", saved["admission_episode_id"])


if __name__ == "__main__":
    unittest.main()
