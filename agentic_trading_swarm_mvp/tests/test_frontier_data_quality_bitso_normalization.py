import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_data_quality import _format_symbol, _normalize_bitso_symbol


class BitsoSymbolNormalizationTests(unittest.TestCase):
    def test_normalize_compact_mxn_symbol(self) -> None:
        self.assertEqual(_normalize_bitso_symbol("BTCMXN"), "btc_mxn")

    def test_normalize_ccxt_style_mxn_symbol(self) -> None:
        self.assertEqual(_normalize_bitso_symbol("BTC/MXN:MXN"), "btc_mxn")

    def test_normalize_book_suffix_symbol(self) -> None:
        self.assertEqual(_normalize_bitso_symbol("btc_mxn_book"), "btc_mxn")

    def test_format_symbol_uses_bitso_normalization(self) -> None:
        self.assertEqual(_format_symbol("BITSO", "BTC/MXN:MXN"), "btc_mxn")


if __name__ == "__main__":
    unittest.main()
