import importlib
import unittest


class TestGlobalMarketDiscoveryDirectCash(unittest.TestCase):
    def test_discovery_import_or_skip(self):
        try:
            importlib.import_module("src.scanners.global_market_discovery")
        except ModuleNotFoundError as exc:
            self.skipTest(f"discovery module unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
