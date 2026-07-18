import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import _infer_code_category, _normalize_code_change_recommendation


class PaperScoringLogicNormalizationTests(unittest.TestCase):
    def test_stale_signal_freshness_guard_maps_to_paper_policy(self) -> None:
        payload = {
            "action": "propose_build_task",
            "title": "Invalidate stale exploit directives for decayed proxy momentum",
            "market_key": "paper_radar.proxy_momentum.staleness_guard",
            "proposed_change": (
                "Add a freshness guard that marks any exploit-like or opportunistic momentum directive "
                "as invalid when its source signal age exceeds the configured horizon or when momentum "
                "drops below the decay threshold. Recompute priority after decay, suppress recommendation "
                "emission when the adjusted confidence falls below the minimum threshold, and emit a "
                "paper-only hold/no-op decision with a stale_signal_decayed reason."
            ),
        }

        normalized = _normalize_code_change_recommendation({"payload": payload, "title": payload["title"]})

        self.assertEqual(_infer_code_category(payload), "paper_scoring_logic")
        self.assertEqual(normalized["payload"]["action"], "propose_code_change")
        self.assertEqual(normalized["payload"]["implementation_mode"], "paper_policy")
        self.assertEqual(normalized["payload"]["code_change"]["change_category"], "paper_scoring_logic")
        self.assertEqual(normalized["payload"]["code_change"]["implementation_mode"], "paper_policy")

    def test_adapter_language_still_prefers_public_data_adapter(self) -> None:
        payload = {
            "action": "request_market_adapter",
            "title": "Expand frontier venue adapter coverage",
            "proposed_change": (
                "Build a public-data exchange adapter with instrument lists, ticker coverage, order book "
                "snapshots, trades, and quote freshness checks for underserved venues."
            ),
        }

        self.assertEqual(_infer_code_category(payload), "public_data_adapter")


if __name__ == "__main__":
    unittest.main()
