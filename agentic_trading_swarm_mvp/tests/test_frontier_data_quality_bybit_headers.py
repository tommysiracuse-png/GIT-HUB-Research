import importlib
import unittest


frontier_data_quality = importlib.import_module("src.frontier_data_quality")


class BybitPublicHeaderProfileTests(unittest.TestCase):
    def test_bybit_browser_headers_extend_default_public_headers(self):
        headers = frontier_data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS
        default_headers = frontier_data_quality._DEFAULT_PUBLIC_HEADERS

        self.assertEqual(headers["User-Agent"], default_headers["User-Agent"])
        self.assertEqual(headers["Accept"], default_headers["Accept"])
        self.assertEqual(headers["Accept-Encoding"], default_headers["Accept-Encoding"])
        self.assertEqual(headers["Origin"], "https://www.bybit.com")
        self.assertEqual(headers["Referer"], "https://www.bybit.com/")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_bybit_header_profile_keeps_browser_client_hints(self):
        headers = frontier_data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS

        self.assertIn("Chromium", headers["Sec-CH-UA"])
        self.assertEqual(headers["Sec-CH-UA-Mobile"], "?0")
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"Windows"')
        self.assertEqual(headers["Priority"], "u=1, i")

    def test_bybit_failover_host_mapping_remains_available(self):
        self.assertEqual(frontier_data_quality._BYBIT_PUBLIC_FAILOVER_HOSTS.get("api.bybit.com"), "api.bytick.com")


if __name__ == "__main__":
    unittest.main()
import unittest

from src import frontier_data_quality as data_quality


class TestBybitPublicHeaders(unittest.TestCase):
    def test_default_public_user_agent_is_browser_like(self):
        headers = data_quality._DEFAULT_PUBLIC_HEADERS
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("Chrome/", headers["User-Agent"])
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_bybit_headers_include_read_only_browser_fields(self):
        headers = data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS
        self.assertEqual(headers["Origin"], "https://www.bybit.com")
        self.assertEqual(headers["Referer"], "https://www.bybit.com/")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertEqual(headers["DNT"], "1")
        self.assertEqual(headers["Sec-Fetch-Dest"], "empty")
        self.assertEqual(headers["Sec-Fetch-Mode"], "cors")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-site")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_bybit_headers_preserve_default_accept_and_user_agent(self):
        self.assertEqual(
            data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS["Accept"],
            data_quality._DEFAULT_PUBLIC_HEADERS["Accept"],
        )
        self.assertEqual(
            data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS["User-Agent"],
            data_quality._DEFAULT_PUBLIC_HEADERS["User-Agent"],
        )
