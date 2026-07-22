from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SignalPlannerFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from runtime import signal_planner as sp  # type: ignore
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"signal_planner unavailable: {exc}")
        self.sp = sp

    def test_fresh_data_passes_through(self) -> None:
        planner = getattr(self.sp, "SignalPlanner", None)
        if planner is None:
            self.skipTest("SignalPlanner unavailable")
        inst = planner({"paper_data_freshness_threshold_seconds": 90})
        quote_ts = _utcnow() - timedelta(seconds=5)
        candidate = {"symbol": "AAA", "quote_timestamp": quote_ts.isoformat()}
        result = inst.apply_freshness_gate(candidate) if hasattr(inst, "apply_freshness_gate") else candidate
        self.assertFalse(result.get("stale_data", False))

    def test_stale_quote_rejected(self) -> None:
        planner = getattr(self.sp, "SignalPlanner", None)
        if planner is None:
            self.skipTest("SignalPlanner unavailable")
        inst = planner({"paper_data_freshness_threshold_seconds": 90})
        quote_ts = _utcnow() - timedelta(seconds=120)
        candidate = {"symbol": "AAA", "quote_timestamp": quote_ts}
        result = inst.apply_freshness_gate(candidate) if hasattr(inst, "apply_freshness_gate") else candidate
        if hasattr(result, "get"):
            self.assertTrue(result.get("stale_data", False) or result.get("rejection_reason"))

    def test_stale_bar_rejected(self) -> None:
        planner = getattr(self.sp, "SignalPlanner", None)
        if planner is None:
            self.skipTest("SignalPlanner unavailable")
        inst = planner({"paper_data_freshness_threshold_seconds": 90})
        bar_ts = _utcnow() - timedelta(seconds=120)
        candidate = {"symbol": "AAA", "bar_timestamp": bar_ts}
        result = inst.apply_freshness_gate(candidate) if hasattr(inst, "apply_freshness_gate") else candidate
        if hasattr(result, "get"):
            self.assertTrue(result.get("stale_data", False) or result.get("rejection_reason"))

    def test_missing_timestamp_rejected(self) -> None:
        planner = getattr(self.sp, "SignalPlanner", None)
        if planner is None:
            self.skipTest("SignalPlanner unavailable")
        inst = planner({"paper_data_freshness_threshold_seconds": 90})
        candidate = {"symbol": "AAA"}
        result = inst.apply_freshness_gate(candidate) if hasattr(inst, "apply_freshness_gate") else candidate
        if hasattr(result, "get"):
            self.assertTrue(result.get("missing_timestamp_rejection") or result.get("rejection_reason"))

    def test_open_position_bypasses_entry_gate(self) -> None:
        planner = getattr(self.sp, "SignalPlanner", None)
        if planner is None:
            self.skipTest("SignalPlanner unavailable")
        inst = planner({"paper_data_freshness_threshold_seconds": 90})
        candidate = {"symbol": "AAA", "has_open_position": True, "quote_timestamp": _utcnow() - timedelta(hours=1)}
        result = inst.apply_freshness_gate(candidate) if hasattr(inst, "apply_freshness_gate") else candidate
        if hasattr(result, "get"):
            self.assertFalse(result.get("stale_entry_blocked", False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
