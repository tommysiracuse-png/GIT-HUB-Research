import datetime as dt
import unittest

from src.frontier_data_quality import _paper_only_parse_timestamp


class PaperOnlyParseTimestampTests(unittest.TestCase):
    def test_accepts_epoch_seconds_string(self):
        raw_value = "1700000000"

        parsed = _paper_only_parse_timestamp(raw_value)

        self.assertEqual(parsed, dt.datetime.fromtimestamp(1700000000, tz=dt.timezone.utc))

    def test_accepts_epoch_milliseconds_string(self):
        raw_value = "1700000000000"

        parsed = _paper_only_parse_timestamp(raw_value)

        self.assertEqual(parsed, dt.datetime.fromtimestamp(1700000000, tz=dt.timezone.utc))

    def test_rejects_boolean_values(self):
        self.assertIsNone(_paper_only_parse_timestamp(True))
        self.assertIsNone(_paper_only_parse_timestamp(False))

    def test_keeps_iso8601_support(self):
        parsed = _paper_only_parse_timestamp("2026-07-31T03:44:27Z")

        self.assertEqual(
            parsed,
            dt.datetime(2026, 7, 31, 3, 44, 27, tzinfo=dt.timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
