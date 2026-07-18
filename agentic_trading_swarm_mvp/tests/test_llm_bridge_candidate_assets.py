import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from llm_bridge import _extract_candidate_rows


class LlmBridgeCandidateAssetParsingTests(unittest.TestCase):
    def test_parses_okx_inst_id_into_asset_context(self):
        report = {
            "normalized_candidates": [
                {
                    "venue_or_source": "OKX",
                    "instId": "BTC-USDT-SWAP",
                }
            ]
        }

        row = _extract_candidate_rows(report)[0]

        self.assertEqual(row["base_asset"], "BTC")
        self.assertEqual(row["quote_asset"], "USDT")
        self.assertEqual(row["normalized_instrument_id"], "BTC-USDT")
        self.assertEqual(row["normalized_symbol"], "BTC/USDT")
        self.assertEqual(row["asset_key"], "okx:BTC/USDT")
        self.assertEqual(row["parse_confidence"], "inst_id")

    def test_parses_compact_symbol_when_inst_id_missing(self):
        report = {
            "candidates": [
                {
                    "venue": "okx",
                    "symbol": "ETHUSDT",
                }
            ]
        }

        row = _extract_candidate_rows(report)[0]

        self.assertEqual(row["base_asset"], "ETH")
        self.assertEqual(row["quote_asset"], "USDT")
        self.assertEqual(row["asset_key"], "okx:ETH/USDT")
        self.assertEqual(row["parse_confidence"], "concatenated_symbol")

    def test_parses_leg_metadata_for_basis_candidates(self):
        report = {
            "top_candidates": [
                {
                    "venue_or_source": "OKX",
                    "legs": [{"instId": "SOL-USDC-SWAP"}, {"instId": "SOL-USDC"}],
                }
            ]
        }

        row = _extract_candidate_rows(report)[0]

        self.assertEqual(row["base_asset"], "SOL")
        self.assertEqual(row["quote_asset"], "USDC")
        self.assertEqual(row["asset_key"], "okx:SOL/USDC")
        self.assertEqual(row["parse_confidence"], "legs")

    def test_normalizes_existing_explicit_assets(self):
        report = {
            "recent_candidates": [{"venue_or_source": "OKX", "base_asset": "ada", "quote_asset": "usdt"}]
        }

        row = _extract_candidate_rows(report)[0]

        self.assertEqual(row["base_asset"], "ADA")
        self.assertEqual(row["quote_asset"], "USDT")
        self.assertEqual(row["asset_key"], "okx:ADA/USDT")
        self.assertEqual(row["parse_confidence"], "explicit")


if __name__ == "__main__":
    unittest.main()
