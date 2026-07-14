import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_data_quality import _normalize_levels


class NormalizeLevelsTrailingPairTests(unittest.TestCase):
    def test_uses_trailing_price_quantity_when_leading_value_looks_like_timestamp(self) -> None:
        levels, anomalies = _normalize_levels(
            [[1718021300123, 7, 0.25, 61234.5]],
            side="bids",
            max_levels=5,
        )
        self.assertEqual(levels, [[61234.5, 0.25]])
        self.assertEqual(anomalies, [])

    def test_preserves_leading_price_quantity_when_already_valid(self) -> None:
        levels, anomalies = _normalize_levels(
            [[61234.5, 0.25, 1718021300123, 7]],
            side="bids",
            max_levels=5,
        )
        self.assertEqual(levels, [[61234.5, 0.25]])
        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
