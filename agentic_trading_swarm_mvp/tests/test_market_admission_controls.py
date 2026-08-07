from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_admission_bridge import run_market_admission_bridge  # noqa: E402


class MarketAdmissionControlTests(unittest.TestCase):
    def test_bridge_subswitch_prevents_all_actions(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            result = run_market_admission_bridge(
                conn,
                {
                    "market_admission": {
                        "enabled": True,
                        "monitor_enabled": True,
                        "paper_queue_enabled": True,
                        "bridge_enabled": False,
                    }
                },
                {
                    "states": [
                        {
                            "admission_key": "test-key",
                            "venue": "OKX",
                            "inst_id": "BTC-USDT-SWAP",
                            "market_surface": "okx_perpetual_swap",
                            "strategy_lineage": "test",
                            "current_stage": "strategy_candidate",
                            "highest_stage": "strategy_candidate",
                            "blocker_code": "route_unknown",
                            "session_status": "continuous",
                            "details": {},
                        }
                    ]
                },
            )
        finally:
            conn.close()

        self.assertFalse(result["summary"]["enabled"])
        self.assertFalse(result["summary"]["bridge_enabled"])
        self.assertEqual([], result["actions"])


if __name__ == "__main__":
    unittest.main()
