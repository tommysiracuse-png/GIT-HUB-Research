from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.canary import run_radar_canary, skip_canary  # noqa: E402


class EvolutionCanaryTests(unittest.TestCase):
    def test_canary_runs_one_radar_iteration_with_current_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp)
            (app / "src").mkdir()
            (app / "runs").mkdir()
            (app / "src" / "radar_loop.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "Path('runs/radar_state_latest.json').write_text(json.dumps({'allow_live_trading': False}), encoding='utf-8')\n"
                "print('ok')\n",
                encoding="utf-8",
            )

            canary = run_radar_canary(app, timeout_seconds=30, max_latency_seconds=30)

            self.assertTrue(canary["passed"], canary)
            self.assertEqual(canary["command"][0], sys.executable)
            self.assertEqual(canary["command"][-2:], ["--iterations", "1"])

    def test_canary_fails_if_live_flag_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp)
            (app / "src").mkdir()
            (app / "runs").mkdir()
            (app / "src" / "radar_loop.py").write_text(
                "from pathlib import Path\n"
                "Path('runs/radar_state_latest.json').write_text('{\"allow_live_trading\": true}', encoding='utf-8')\n",
                encoding="utf-8",
            )

            canary = run_radar_canary(app, timeout_seconds=30, max_latency_seconds=30)

            self.assertFalse(canary["passed"])
            self.assertFalse(canary["live_flag_ok"])

    def test_skip_canary_records_passed_skip_reason(self) -> None:
        skipped = skip_canary("unit")
        self.assertEqual(skipped["reason"], "unit")
        self.assertEqual(skipped["stage"], "deferred_by_policy")


if __name__ == "__main__":
    unittest.main()
