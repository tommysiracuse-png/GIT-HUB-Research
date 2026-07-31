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

import hunter_allocation  # noqa: E402
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

    def test_global_discovery_directives_get_explore_metadata_and_report(self) -> None:
        candidates = [
            {"inst_id": "BITSO-BTC-MXN", "venue": "Bitso", "signal_key": "regional fiat crypto", "score": 90},
            {"inst_id": "OKX-BTC-SWAP", "venue": "OKX", "signal_key": "OKX funding", "score": 89},
        ]
        directives = [
            {
                "id": 42,
                "market_key": "global_discovery|Bitso",
                "directive": "global_market_discovery",
                "priority": 91,
                "rationale": "Regional fiat market needs exploration slots.",
            }
        ]

        selected, report = allocate_candidate_review(
            candidates,
            directives,
            2,
            buckets={"exploit": 0.0, "explore": 0.5, "diagnose": 0.0},
        )

        bitso = next(row for row in selected if row["inst_id"] == "BITSO-BTC-MXN")
        self.assertEqual(bitso["_hunter_bucket"], "explore")
        self.assertEqual(bitso["_hunter_directive_id"], 42)
        self.assertIn("exploration slots", bitso["_hunter_allocation_reason"])

        with tempfile.TemporaryDirectory() as tmp:
            old_json = hunter_allocation.REPORT_JSON
            old_md = hunter_allocation.REPORT_MD
            old_discovery = hunter_allocation.DISCOVERY_JSONL
            try:
                hunter_allocation.REPORT_JSON = pathlib.Path(tmp) / "hunter_allocation_report.json"
                hunter_allocation.REPORT_MD = pathlib.Path(tmp) / "hunter_allocation_report.md"
                hunter_allocation.DISCOVERY_JSONL = pathlib.Path(tmp) / "market_discovery_candidates.jsonl"
                written = hunter_allocation.write_hunter_allocation_report(report, selected, candidates, {})
                self.assertTrue(hunter_allocation.REPORT_JSON.exists())
                self.assertEqual(written["selected_by_bucket"]["explore"], 1)
                self.assertIn("Global discoveries", hunter_allocation.REPORT_MD.read_text(encoding="utf-8"))
            finally:
                hunter_allocation.REPORT_JSON = old_json
                hunter_allocation.REPORT_MD = old_md
                hunter_allocation.DISCOVERY_JSONL = old_discovery

    def test_global_discovery_candidates_receive_explore_floor_without_directive(self) -> None:
        candidates = [
            {"inst_id": "OKX-BTC-SWAP", "venue": "OKX", "signal_key": "OKX funding", "score": 99},
            {
                "inst_id": "B3:EWZ",
                "venue": "B3",
                "trade_type": "global_market_discovery_proxy",
                "market_surface": "global_market_discovery",
                "market_key": "global_discovery|B3",
                "score": 42,
            },
            {"inst_id": "MEXC-XYZ-USDT", "venue": "MEXC", "signal_key": "frontier", "score": 98},
        ]

        selected, report = allocate_candidate_review(
            candidates,
            [],
            2,
            buckets={"exploit": 0.0, "explore": 0.5, "diagnose": 0.0},
        )

        b3 = next(row for row in selected if row["inst_id"] == "B3:EWZ")
        self.assertEqual(b3["_hunter_bucket"], "explore")
        self.assertEqual(b3["_hunter_allocation_reason"], "global_discovery_exploration_floor")
        self.assertEqual(report["selected_by_bucket"]["explore"], 1)

    def test_global_discovery_floor_reserves_four_explore_slots_when_available(self) -> None:
        candidates = [
            {"inst_id": "OKX-BTC-SWAP", "venue": "OKX", "signal_key": "OKX funding", "score": 99},
            {"inst_id": "MEXC-XYZ-USDT", "venue": "MEXC", "signal_key": "frontier", "score": 98},
        ]
        for idx, venue in enumerate(("B3", "LONDON_STOCK_EXCHANGE", "TMX_GROUP", "CBOE_GLOBAL_MARKETS", "HKEX"), start=1):
            candidates.append(
                {
                    "inst_id": f"{venue}:PROXY{idx}",
                    "venue": venue,
                    "trade_type": "global_market_discovery_proxy",
                    "market_surface": "global_market_discovery",
                    "market_key": f"global_discovery|{venue}",
                    "score": 60 - idx,
                }
            )

        selected, report = allocate_candidate_review(
            candidates,
            [],
            6,
            buckets={"exploit": 0.0, "explore": 1.0, "diagnose": 0.0},
        )

        floor_selected = [
            row for row in selected if row.get("_hunter_allocation_reason") == "global_discovery_exploration_floor"
        ]
        self.assertGreaterEqual(len(floor_selected), 4)
        self.assertEqual(report["selected_by_bucket"]["explore"], 5)

    def test_global_discovery_report_merges_case_only_region_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_discovery = hunter_allocation.DISCOVERY_JSONL
            try:
                hunter_allocation.DISCOVERY_JSONL = pathlib.Path(tmp) / "market_discovery_candidates.jsonl"
                rows = [
                    {
                        "region": "Global",
                        "surface_type_classified": "Listed Derivatives",
                        "created_at": "2026-07-30T20:00:00+00:00",
                    },
                    {
                        "region": "global",
                        "surface_type_classified": "listed derivatives",
                        "created_at": "2026-07-30T20:00:00+00:00",
                    },
                ]
                hunter_allocation.DISCOVERY_JSONL.write_text(
                    "\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8",
                )

                report = hunter_allocation._global_discovery_counts()
            finally:
                hunter_allocation.DISCOVERY_JSONL = old_discovery

        self.assertEqual(report["by_region"], {"global": 2})
        self.assertEqual(len(report["by_surface_type"]), 1)
        self.assertEqual(sum(report["by_surface_type"].values()), 2)


if __name__ == "__main__":
    unittest.main()
