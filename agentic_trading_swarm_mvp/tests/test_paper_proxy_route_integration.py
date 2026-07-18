import importlib
import unittest


class PaperProxyRouteIntegrationTests(unittest.TestCase):
    def test_route_resolver_imports_in_isolation(self):
        module = importlib.import_module("src.route_resolver")
        self.assertTrue(hasattr(module, "RUNS_DIR"))

    def test_tests_package_import_resolves(self):
        module = importlib.import_module("tests.test_paper_proxy_route_integration")
        self.assertTrue(hasattr(module, "PaperProxyRouteIntegrationTests"))


if __name__ == "__main__":
    unittest.main()
