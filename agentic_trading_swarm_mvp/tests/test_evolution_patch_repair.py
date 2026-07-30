import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import _consumer_validation_audit


class ConsumerValidationAuditTests(unittest.TestCase):
    def test_reuses_suppression_window_for_same_invalid_payload(self) -> None:
        rec = {}
        payload = {
            "source_agent": "cross_market_researcher",
            "title": "invalid expected files",
        }

        first = _consumer_validation_audit(
            rec,
            payload,
            invalid_expected_files=["../outside.py"],
            invalid_implementation_mode=None,
        )
        second = _consumer_validation_audit(
            rec,
            payload,
            invalid_expected_files=["../outside.py"],
            invalid_implementation_mode=None,
        )

        self.assertEqual(first["proposal_fingerprint"], second["proposal_fingerprint"])
        self.assertEqual(first["suppressed_until"], second["suppressed_until"])
        self.assertEqual(first["rejection_reason"], "disallowed_expected_files")
