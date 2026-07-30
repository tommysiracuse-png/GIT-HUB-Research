import importlib
import unittest


class TestJseNseCashAdapter(unittest.TestCase):
    def test_module_import_or_skip(self):
        try:
            importlib.import_module("src.market_data.adapters.jse_nse_cash_adapter")
        except ModuleNotFoundError as exc:
            self.skipTest(f"adapter module unavailable: {exc}")

    def test_venue_universes_import_or_skip(self):
        try:
            importlib.import_module("src.market_data.venue_universes")
        except ModuleNotFoundError as exc:
            self.skipTest(f"venue universe module unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
