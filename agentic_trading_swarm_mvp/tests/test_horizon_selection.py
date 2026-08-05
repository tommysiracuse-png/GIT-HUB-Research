from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from horizon_selection import candidate_horizons, select_sticky_horizon


class HorizonSelectionTests(unittest.TestCase):
    def test_explicit_fixed_horizon_is_respected(self) -> None:
        settings = {
            "learning": {"horizon_minutes": [5, 15, 60, 240, 1440]},
            "strategy_lab": {"evaluation_horizon_mode": "best_reliable"},
        }

        self.assertEqual(
            [240],
            candidate_horizons(
                settings,
                "strategy_lab",
                {"horizon_mode": "fixed", "horizon_minutes": 240},
            ),
        )

    def test_selected_horizon_is_sticky_until_uplift_is_material(self) -> None:
        evaluations = {
            60: {"passed": True, "ready": True, "selection_score_bps": 10.0, "evidence_count": 50},
            240: {"passed": True, "ready": True, "selection_score_bps": 14.0, "evidence_count": 50},
        }

        retained = select_sticky_horizon(evaluations, 60, switch_uplift_bps=6.0)
        self.assertEqual(60, retained["selected_horizon_minutes"])
        self.assertEqual("sticky_horizon_retained", retained["selection_reason"])

        evaluations[240]["selection_score_bps"] = 17.0
        switched = select_sticky_horizon(evaluations, 60, switch_uplift_bps=6.0)
        self.assertEqual(240, switched["selected_horizon_minutes"])
        self.assertEqual("material_horizon_uplift", switched["selection_reason"])


if __name__ == "__main__":
    unittest.main()
