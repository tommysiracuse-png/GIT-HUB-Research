import json
import sys
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_order_router
import strategy_reliability


class PaperFamilyQuarantineTests(unittest.TestCase):
    @staticmethod
    def _candidate() -> dict[str, str]:
        return {
            "market_key": "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            "signal_family": "yahoo_proxy",
            "strategy": "global_proxy_momentum",
            "signal_key": "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            "venue": "TEST",
            "direction": "long",
        }

    def test_strategy_reliability_quarantines_exact_market_key(self) -> None:
        helper = getattr(strategy_reliability, "paper_family_quarantine_record", None)
        self.assertTrue(callable(helper))

        record = helper(self._candidate())
        self.assertIsInstance(record, dict)
        self.assertTrue(record)
        self.assertIn("quarantine", json.dumps(record, sort_keys=True, default=str).lower())

    def test_router_shadows_quarantined_family_before_frontier_checks(self) -> None:
        reason = paper_order_router.frontier_shadow_filter_reason(self._candidate())

        self.assertIsInstance(reason, dict)
        self.assertFalse(reason.get("paper_fill_allowed", True))
        self.assertEqual(
            reason.get("candidate", {}).get("market_key"),
            "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
        )
        self.assertIn("quarantine", json.dumps(reason, sort_keys=True, default=str).lower())

    def test_router_normalizes_minimal_family_quarantine_record(self) -> None:
        with mock.patch.object(
            strategy_reliability,
            "paper_family_quarantine_record",
            return_value={"reason": "paper_strategy_family_quarantine"},
        ):
            reason = paper_order_router.frontier_shadow_filter_reason(self._candidate())

        self.assertEqual(reason.get("guard"), "paper_strategy_family_quarantine")
        self.assertFalse(reason.get("paper_fill_allowed", True))
        self.assertEqual(reason.get("candidate", {}).get("market_key"), "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM")


if __name__ == "__main__":
    unittest.main()
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from paper_order_router import apply_frontier_paper_guard
from strategy_reliability import paper_family_quarantine_record


class PaperFamilyQuarantineTests(unittest.TestCase):
    def test_quarantines_base_lineage_family(self):
        candidate = {
            "strategy_id": "STRATEGY_LAB|candidate_alpha",
            "lineage": ["seed:YAHOO_PROXY", "family:global_proxy_momentum", "mutation:v3"],
        }

        record = paper_family_quarantine_record(candidate)

        self.assertIsNotNone(record)
        self.assertEqual(record["reason"], "quarantined_family_decay")
        self.assertTrue(record["paper_only"])
        self.assertIn("lineage", record["matched_fields"])

    def test_quarantines_known_descendant_even_without_full_base_terms(self):
        candidate = {
            "parent_strategy_id": "STRATEGY_LAB|red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
            "strategy_id": "STRATEGY_LAB|descendant_candidate",
        }

        record = paper_family_quarantine_record(candidate)

        self.assertIsNotNone(record)
        self.assertIn(
            "strategy_lab|red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
            record["matched_descendants"],
        )

    def test_router_shadow_filters_quarantined_candidate(self):
        candidate = {
            "strategy": "experimental_proxy_rotation",
            "parent_strategy_id": "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975",
            "paper_filled": True,
            "status": "paper_filled",
        }

        guarded = apply_frontier_paper_guard(candidate)

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_filled"])
        self.assertEqual(guarded["status"], "shadow_filtered")
        self.assertEqual(guarded["candidate_reject_reason"], "quarantined_family_decay")
        self.assertIn("paper_strategy_quarantine", guarded)

    def test_quarantine_can_be_disabled_by_paper_flag(self):
        candidate = {"lineage": "YAHOO_PROXY/global_proxy_momentum/retry"}
        guarded = apply_frontier_paper_guard(candidate, {"paper_strategy_family_quarantine_enabled": False})
        self.assertFalse(guarded.get("shadow_filtered", False))


if __name__ == "__main__":
    unittest.main()
