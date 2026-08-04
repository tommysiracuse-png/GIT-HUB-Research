"""Shared parity contract used by autonomously promoted Strategy Lab plugins."""

from __future__ import annotations

import copy
import datetime as dt
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS  # noqa: E402
from strategy_program import assert_plugin_parity, generate_program_candidates  # noqa: E402


class GeneratedStrategyParityContractTests(unittest.TestCase):
    def test_delegating_generated_plugin_matches_program_interpreter(self) -> None:
        program = {
            "type": "observation_program",
            "universe": {"venues": ["TEST"]},
            "entry_expression": "quality_score >= 60",
            "long_expression": "return_5m_bps > 0",
            "short_expression": "return_5m_bps < 0",
            "edge_expression": "abs(return_5m_bps) - spread_bps",
            "score_expression": "clip(50 + abs(return_5m_bps), 0, 100)",
            "route_surface": "proxy",
        }
        experiment = {
            "strategy_lab_id": "generated_fixture",
            "version": 1,
            "hypothesis": "Fixture parity",
            "strategy_logic": program,
        }
        frames = [
            {
                "venue": "TEST",
                "inst_id": "TEST:ABC",
                "trade_type": "global_market_discovery_proxy",
                "last": 100.0,
                "return_5m_bps": 10.0,
                "spread_bps": 2.0,
                "quality_score": 80.0,
                "liquidity_score": 0.8,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        ]
        cfg = copy.deepcopy(DEFAULT_SETTINGS)

        class GeneratedPlugin:
            @staticmethod
            def generate(_observations, context=None):
                return generate_program_candidates(
                    context["strategy_lab_experiment"],
                    context["feature_frames"],
                    context["settings"],
                )[0]

        assert_plugin_parity(GeneratedPlugin, experiment, frames, cfg)


if __name__ == "__main__":
    unittest.main()
