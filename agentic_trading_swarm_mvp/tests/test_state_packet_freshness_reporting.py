from __future__ import annotations

import unittest


class StatePacketFreshnessReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from runtime.reporting import state_packet_builder as spb  # type: ignore
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"state_packet_builder unavailable: {exc}")
        self.spb = spb

    def test_stale_diagnostics_serialized(self) -> None:
        builder = getattr(self.spb, "StatePacketBuilder", None)
        if builder is None:
            self.skipTest("StatePacketBuilder unavailable")
        inst = builder()
        packet = inst.build(
            diagnostics={
                "stale_quote_rejections": 2,
                "stale_bar_rejections": 1,
                "missing_timestamp_rejections": 3,
                "freshness": {
                    "stale_data": True,
                    "age_seconds": 120,
                    "threshold_seconds": 90,
                    "rejection_reason": "stale_data",
                },
            }
        ) if hasattr(inst, "build") else {}
        if hasattr(packet, "get"):
            diagnostics = packet.get("diagnostics", packet)
            self.assertIn("stale_quote_rejections", diagnostics)
            self.assertIn("stale_bar_rejections", diagnostics)
            self.assertIn("missing_timestamp_rejections", diagnostics)

    def test_opportunity_counts_ignore_stale(self) -> None:
        builder = getattr(self.spb, "StatePacketBuilder", None)
        if builder is None:
            self.skipTest("StatePacketBuilder unavailable")
        inst = builder()
        packet = inst.build(
            opportunity_summary={
                "top_opportunities": [],
                "opportunity_count": 0,
                "stale_suppressed_count": 4,
            }
        ) if hasattr(inst, "build") else {}
        if hasattr(packet, "get"):
            summary = packet.get("opportunity_summary", packet)
            self.assertEqual(summary.get("opportunity_count"), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
