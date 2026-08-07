from __future__ import annotations

import copy
import importlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS  # noqa: E402
from signals import registry as signal_registry  # noqa: E402
from signals.runtime import run_signal_plugins  # noqa: E402
from storage import init_db  # noqa: E402
from strategy_lab import (  # noqa: E402
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _settings(*, plugins_enabled: bool) -> dict:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["allow_live_trading"] = False
    settings["strategy_lab"]["promoted_signal_plugins_enabled"] = plugins_enabled
    return settings


def _observation_program_recommendation() -> dict:
    return {
        "recommendation_id": "rec_plugin_kill_switch",
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "title": "Exercise the promoted-plugin kill switch",
            "rationale": "Keep deterministic Strategy Lab compilation independent of plugins.",
            "strategy_lab_experiment": {
                "strategy_lab_id": "plugin_kill_switch_program",
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": "A fixture observation program exercises compilation.",
                "source_surface": "proxy",
                "permitted_target_surface": ["proxy"],
                "strategy_logic": {
                    "type": "observation_program",
                    "universe": {"venues": ["YAHOO_PROXY"]},
                    "calculated_features": {
                        "net_momentum": "return_5m_bps - spread_bps"
                    },
                    "entry_expression": "quality_score >= 60 and net_momentum > 5",
                    "invalidation_expression": "stale_minutes > 5",
                    "long_expression": "net_momentum > 0",
                    "short_expression": "net_momentum < -20",
                    "edge_expression": "max(net_momentum, 0)",
                    "score_expression": "clip(50 + net_momentum / 2, 0, 100)",
                    "route_surface": "proxy",
                },
                "data_requirements": {"paper_only": True},
                "risk_gates": {},
                "promotion_rules": {},
            },
        },
    }


class PromotedSignalKillSwitchTests(unittest.TestCase):
    def test_disabled_switch_prevents_runtime_lifecycle_and_compilation_imports(self) -> None:
        module_name = "signals.generated.kill_switch_side_effect_fixture"
        signal_id = "kill_switch_side_effect_fixture"
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_dir = pathlib.Path(tmpdir)
            marker = fixture_dir / "imported.txt"
            (fixture_dir / "kill_switch_side_effect_fixture.py").write_text(
                "from pathlib import Path\n"
                "from signals.base import SignalInfo\n"
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
                f"info = SignalInfo(signal_id={signal_id!r}, family='fixture', version='1')\n"
                "def generate(observations, context=None):\n"
                "    return []\n",
                encoding="utf-8",
            )

            generated_package = importlib.import_module("signals.generated")
            generated_package.__path__.append(str(fixture_dir))
            try:
                disabled = _settings(plugins_enabled=False)
                with _connection() as conn:
                    ingest_strategy_lab_recommendation(
                        conn, _observation_program_recommendation(), disabled
                    )
                    run_signal_plugins(conn, [], disabled)
                    generate_strategy_lab_candidates(conn, disabled, [])

                    self.assertFalse(marker.exists())
                    self.assertNotIn(module_name, sys.modules)

                    enabled = _settings(plugins_enabled=True)
                    generate_strategy_lab_candidates(conn, enabled, [])

                self.assertTrue(marker.exists())
                self.assertIn(module_name, sys.modules)
            finally:
                generated_package.__path__.remove(str(fixture_dir))
                sys.modules.pop(module_name, None)
                signal_registry._SIGNALS.pop(signal_id, None)


if __name__ == "__main__":
    unittest.main()
