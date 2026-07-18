import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import frontier_crypto_adapter as fca


class ProxySignalFreshnessGateTests(unittest.TestCase):
    def test_stale_proxy_bar_suppresses_yahoo_proxy_context(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:40:00+00:00",
            previous_bar_timestamp="2026-07-18T04:39:00+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:00+00:00",
            basis_deviation_bps=12.0,
            mapping_confidence=0.85,
        )

        self.assertTrue(gate["applicable"])
        self.assertFalse(gate["eligible"])
        self.assertFalse(gate["emit_recommendation"])
        self.assertEqual(gate["fail_closed_reason"], "stale_proxy_bar")
        self.assertEqual(gate["suppressed_signal_count"], 1)
        self.assertGreater(gate["proxy_bar_age_ms"], gate["max_bar_age_ms"])

    def test_fresh_proxy_context_remains_eligible(self):
        gate = fca.paper_only_proxy_signal_freshness_gate(
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            latest_bar_timestamp="2026-07-18T04:47:10+00:00",
            previous_bar_timestamp="2026-07-18T04:46:10+00:00",
            scheduler_timestamp="2026-07-18T04:48:00+00:00",
            source_timestamp="2026-07-18T04:47:25+00:00",
            basis_deviation_bps=14.5,
            mapping_confidence=0.82,
        )

        self.assertTrue(gate["eligible"])
        self.assertTrue(gate["emit_recommendation"])
        self.assertIsNone(gate["fail_closed_reason"])
        self.assertEqual(gate["suppressed_signal_count"], 0)

    def test_signal_quality_gate_fail_closes_when_proxy_gate_fails(self):
        result = fca.paper_only_cross_market_signal_quality_gate(
            confidence=0.91,
            primary_trigger_present=True,
            related_market_confirmed=True,
            signal_age_ms=500.0,
            market_key="YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            proxy_signal_freshness_gate={
                "applicable": True,
                "eligible": False,
                "fail_closed_reason": "basis_deviation_exceeded",
                "suppressed_signal_count": 1,
                "state": "observe_only",
            },
        )

        self.assertFalse(result["promote"])
        self.assertTrue(result["observe_only"])
        self.assertEqual(result["fail_closed_reason"], "basis_deviation_exceeded")
        self.assertEqual(result["suppressed_signal_count"], 1)
        self.assertEqual(result["state"], "observe_only")

    def test_validate_payload_converts_failed_proxy_gate_to_hold(self):
        payload = {
            "action": "propose_code_change",
            "title": "Proxy momentum packet",
            "market_key": "YAHOO_PROXY_GLOBAL_PROXY_MOMENTUM",
            "priority": 93,
            "confidence": 0.9,
            "evidence": {
                "paper_only_proxy_signal_freshness_gate": {
                    "applicable": True,
                    "eligible": False,
                    "emit_recommendation": False,
                    "fail_closed_reason": "mapping_confidence_too_low",
                    "fail_closed_reasons": ["mapping_confidence_too_low"],
                    "proxy_bar_age_ms": 42000.0,
                    "source_timestamp_lag_ms": 35000.0,
                    "basis_deviation_bps": 10.0,
                    "mapping_confidence": 0.42,
                    "suppressed_signal_count": 1,
                }
            },
            "proposed_change": {"objective": "suppress stale proxy signals"},
            "rationale": "Fail closed when proxy mapping quality is too low.",
        }

        validated = fca.validate_paper_recommendation_payload(payload)

        self.assertEqual(validated["action"], "hold")
        self.assertEqual(validated["evidence"]["issue_type"], "proxy_signal_freshness_failed")
        self.assertEqual(validated["evidence"]["fail_closed_reason"], "mapping_confidence_too_low")
        self.assertEqual(validated["evidence"]["suppressed_signal_count"], 1)


if __name__ == "__main__":
    unittest.main()
