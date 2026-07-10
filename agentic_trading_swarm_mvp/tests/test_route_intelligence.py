from __future__ import annotations

import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from route_intelligence import (  # noqa: E402
    ROUTE_REQUIREMENT_FIELDS,
    build_route_requirements_matrix,
    render_route_requirements_markdown,
    route_requirements_json,
)


class RouteIntelligenceTests(unittest.TestCase):
    def test_spot_borrow_routes_are_paper_only_prioritized_with_unknowns(self) -> None:
        rows = build_route_requirements_matrix(
            [
                {
                    "venue": "POLYMARKET",
                    "inst_id": "POLYMARKET:EXAMPLE",
                    "direction": "prediction_market",
                    "route_blockers": [
                        "jurisdiction_eligibility",
                        "prediction_markets_account",
                        "venue_api_access",
                    ],
                },
                {
                    "inst_id": "GATE:DEXE_USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "inst_id": "GATE:ARC_USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
                {
                    "inst_id": "COINBASE:XRP-USDT",
                    "direction": "short_frontier_spot",
                    "route_blockers": ["spot_borrow"],
                },
            ]
        )

        self.assertEqual(
            [row["inst_id"] for row in rows[:3]],
            ["GATE:ARC_USDT", "GATE:DEXE_USDT", "COINBASE:XRP-USDT"],
        )
        arc = rows[0]
        self.assertEqual(set(ROUTE_REQUIREMENT_FIELDS), set(arc))
        self.assertTrue(arc["paper_route_only"])
        self.assertTrue(arc["borrow_required"])
        self.assertEqual(arc["borrow_asset"], "ARC")
        self.assertEqual(arc["borrow_fee_bps_estimate_or_unknown"], "unknown")
        self.assertEqual(arc["fee_bps_per_side_or_unknown"], "unknown")
        self.assertEqual(arc["slippage_bps_per_side_or_unknown"], "unknown")
        self.assertEqual(arc["margin_required"], "unknown")
        self.assertEqual(arc["route_status"], "blocked_until_requirements_confirmed")

    def test_polymarket_requirements_remain_blocked_and_no_credentials(self) -> None:
        opportunity = {
            "venue": "POLYMARKET",
            "inst_id": "POLYMARKET:EVENT",
            "direction": "prediction_market",
            "route_blockers": [
                "jurisdiction_eligibility",
                "prediction_markets_account",
                "venue_api_access",
            ],
        }

        row = build_route_requirements_matrix([opportunity])[0]
        self.assertEqual(row["venue"], "POLYMARKET")
        self.assertTrue(row["paper_route_only"])
        self.assertIn("prediction_markets_account", row["route_blockers"])
        self.assertIn("prediction_markets", row["required_account_type"])
        self.assertIn("jurisdiction", row["jurisdiction_requirement"])
        self.assertIn("no_credentials", row["venue_api_requirement"])

        markdown = render_route_requirements_markdown([opportunity])
        self.assertIn("Paper-only read-only output", markdown)
        self.assertIn("No credentials", markdown)
        self.assertTrue(json.loads(route_requirements_json([opportunity]))["paper_only"])
