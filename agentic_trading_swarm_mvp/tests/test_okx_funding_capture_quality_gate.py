import copy
import datetime as dt
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402
from strategy_lab import generate_strategy_lab_candidates  # noqa: E402


class TestOkxFundingCaptureQualityGate(unittest.TestCase):
    def _connection(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        return conn

    def _recommendation(self):
        return {
            "recommendation_id": "okx_quality_gate_rec",
            "source_agent": "strategy_lab",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "title": "OKX funding capture quality gate",
                "rationale": "Tighten freshness, spread, liquidity, and confidence filters for OKX perp funding capture.",
                "variant_config": {
                    "mode": "funding_capture_short_perp_only",
                    "filters": {
                        "freshness_horizon_seconds": 600,
                        "max_entry_spread_bps": 6,
                        "liquidity_floor_usd": 50000,
                        "min_liquidity_score": 0.7,
                        "confidence_floor": 0.62,
                        "require_funding_carry_alignment": True,
                    },
                },
            },
        }

    def test_wrapper_creates_valid_market_strategy_contract(self):
        with self._connection() as conn:
            result = self_improvement.ingest_strategy_lab_recommendation(
                conn,
                self._recommendation(),
            )
            row = conn.execute(
                """
                select experiment_type, status, strategy_logic_json, risk_gates_json
                from strategy_lab_experiments
                """
            ).fetchone()

        self.assertEqual("created", result[0]["action_status"])
        self.assertEqual("market_strategy", row["experiment_type"])
        self.assertEqual("proposed", row["status"])
        self.assertIn("funding_capture_short_perp", row["strategy_logic_json"])
        self.assertIn("allowed_field_values", row["risk_gates_json"])

    def test_quality_gate_generates_only_aligned_liquid_candidate(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["allow_live_trading"] = False
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "direction": "funding_capture_short_perp",
            "trade_type": "perp_funding_basis",
            "score": 80.0,
            "liquidity_score": 0.85,
            "quote_volume_24h": 1000000.0,
            "spread_bps": 2.0,
            "funding_bps": 12.0,
            "basis_bps": 3.0,
            "carry_alignment_status": "carry_aligned_positive",
            "seen_at": now,
            "last": 100.0,
            "route_status": "standard",
        }
        with self._connection() as conn:
            self_improvement.ingest_strategy_lab_recommendation(conn, self._recommendation())
            generated, report = generate_strategy_lab_candidates(conn, settings, [candidate])
            rejected, _ = generate_strategy_lab_candidates(
                conn,
                settings,
                [{**candidate, "carry_alignment_status": "cost_eroded"}],
            )

        self.assertEqual(1, len(generated))
        self.assertEqual(1, report["generated_candidates"])
        self.assertEqual([], rejected)
        self.assertTrue(generated[0]["strategy_lab_id"].startswith("okx_funding_capture_quality_gate_"))


if __name__ == "__main__":
    unittest.main()
