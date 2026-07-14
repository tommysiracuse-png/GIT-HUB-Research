import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_data_quality import _build_depth_url, _format_symbol


class BybitDepthFormattingTests(unittest.TestCase):
    def test_format_symbol_compacts_bybit_spot_pairs(self) -> None:
        self.assertEqual(_format_symbol("BYBIT_SPOT", "btc/usdt"), "BTCUSDT")
        self.assertEqual(_format_symbol("BYBIT_SPOT", "btc-usdt"), "BTCUSDT")

    def test_build_depth_url_uses_compact_bybit_spot_symbol(self) -> None:
        observation = {"venue": "BYBIT_SPOT", "symbol": "BTC/USDT"}
        depth_config = {
            "url_template": "https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}&limit={limit}",
            "max_levels": 50,
        }
        self.assertEqual(
            _build_depth_url(observation, depth_config, levels=200),
            "https://api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=50",
        )

    def test_build_depth_url_uses_compact_bybit_linear_symbol(self) -> None:
        observation = {"venue": "BYBIT", "symbol": "BTC-USDT"}
        depth_config = {
            "url_template": "https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit={limit}",
            "max_levels": 25,
        }
        self.assertEqual(
            _build_depth_url(observation, depth_config, levels=10),
            "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=10",
        )
