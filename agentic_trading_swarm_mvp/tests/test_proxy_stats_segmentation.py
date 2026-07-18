import importlib
import unittest


class ProxyStatsSegmentationTests(unittest.TestCase):
    def test_route_resolver_module_available(self):
        module = importlib.import_module("src.route_resolver")
        self.assertIsNotNone(module)

    def test_proxy_test_module_is_importable(self):
        module = importlib.import_module("tests.test_proxy_stats_segmentation")
        self.assertTrue(hasattr(module, "ProxyStatsSegmentationTests"))


if __name__ == "__main__":
    unittest.main()
