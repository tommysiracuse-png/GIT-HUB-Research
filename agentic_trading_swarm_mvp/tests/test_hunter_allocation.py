from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hunter_allocation import allocate_candidate_review, allocate_review_slots, classify_directive  # noqa: E402


class HunterAllocationTests(unittest.TestCase):
    def test_classifies_exploit_explore_and_diagnose_directives(self) -> None:
        self.assertEqual(classify_directive({"directive": "exploit positive OKX funding"}), "exploit")
        self.assertEqual(classify_directive({"directive": "diagnose weak KUCOIN long"}), "diagnose")
        self.assertEqual(classify_directive({"directive": "research new regional venue"}), "explore")

    def test_allocates_reserved_review_slots(self) -> None:
        directives = [
            {"directive": "exploit positive", "priority": 90},
            {"directive": "diagnose weak", "priority": 80},
            {"directive": "explore new venue", "priority": 70},
        ]

        allocation = allocate_review_slots(directives, 10, buckets={"exploit": 0.5, "explore": 0.3, "diagnose": 0.2})

        self.assertEqual(allocation["slot_targets"], {"exploit": 5, "explore": 3, "diagnose": 2})
        self.assertEqual(allocation["directive_counts"], {"exploit": 1, "explore": 1, "diagnose": 1})

    def test_candidate_review_uses_directives_before_fallback(self) -> None:
        candidates = [
            {"inst_id": "OKX-BTC-SWAP", "signal_key": "OKX funding", "score": 99},
            {"inst_id": "KUCOIN-ABC-USDT", "signal_key": "KUCOIN long", "score": 98},
            {"inst_id": "BITSO-USD-MXN", "signal_key": "regional venue", "score": 97},
            {"inst_id": "MEXC-XYZ-USDT", "signal_key": "frontier", "score": 96},
        ]
        directives = [
            {"market_key": "OKX", "directive": "exploit positive", "priority": 90},
            {"market_key": "KUCOIN", "directive": "diagnose weak", "priority": 80},
            {"market_key": "BITSO", "directive": "explore regional", "priority": 70},
        ]

        selected, report = allocate_candidate_review(
            candidates,
            directives,
            4,
            buckets={"exploit": 0.25, "explore": 0.25, "diagnose": 0.25},
        )

        buckets = {row["inst_id"]: row["_hunter_bucket"] for row in selected}
        self.assertEqual(buckets["OKX-BTC-SWAP"], "exploit")
        self.assertEqual(buckets["KUCOIN-ABC-USDT"], "diagnose")
        self.assertEqual(buckets["BITSO-USD-MXN"], "explore")
        self.assertEqual(report["selected_count"], 4)
        self.assertEqual(report["selected_by_bucket"]["fallback"], 1)


if __name__ == "__main__":
    unittest.main()
