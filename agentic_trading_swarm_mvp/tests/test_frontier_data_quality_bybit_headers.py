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
