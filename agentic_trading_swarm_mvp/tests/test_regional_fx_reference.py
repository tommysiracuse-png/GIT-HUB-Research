from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import regional_fx_reference as fx
import storage
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["frontier_regional_fx"]["quotes"] = ["ZAR", "NGN"]
    cfg["frontier_regional_fx"]["cache_ttl_minutes"] = 60
    return cfg


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


class RegionalFxReferenceTests(unittest.TestCase):
    def test_default_quotes_cover_major_frontier_fiat_pairs(self) -> None:
        self.assertIn("AUD", fx.DEFAULT_QUOTES)
        self.assertIn("EUR", fx.DEFAULT_QUOTES)
        self.assertIn("GBP", fx.DEFAULT_QUOTES)
        self.assertIn("TZS", fx.DEFAULT_QUOTES)
        self.assertIn("UGX", fx.DEFAULT_QUOTES)

    def test_default_settings_request_frontier_fiat_fx_for_new_quotes(self) -> None:
        configured = set(DEFAULT_SETTINGS["frontier_regional_fx"]["quotes"])
        self.assertIn("TZS", configured)
        self.assertIn("UGX", configured)
        self.assertEqual(5, DEFAULT_SETTINGS["frontier_regional_fx"]["cache_ttl_minutes"])

    def test_parse_exchange_rate_api_open_response(self) -> None:
        rows = fx.parse_exchange_rate_api(
            {
                "time_last_update_unix": 1_700_000_000,
                "time_next_update_unix": 1_700_086_400,
                "rates": {"ZAR": 18.5, "NGN": "1500.0", "BAD": 0},
            },
            "2026-06-30T00:00:00+00:00",
            {"ZAR", "NGN"},
        )

        self.assertEqual({row["quote"] for row in rows}, {"ZAR", "NGN"})
        self.assertTrue(all(row["provider"] == "ExchangeRate-API Open" for row in rows))
        self.assertTrue(all(row["rate"] > 0 for row in rows))

    def test_fetch_uses_cache_and_writes_latest_report(self) -> None:
        old_fetch = fx.fetch_json
        old_report = fx.REPORT_JSON
        calls = []

        def fake_fetch(url: str, timeout: int = 10):
            calls.append(url)
            return {"rates": {"ZAR": 18.0, "NGN": 1500.0}, "time_last_update_unix": 1_700_000_000}

        with tempfile.TemporaryDirectory() as tmp:
            conn = memory_conn()
            fx.fetch_json = fake_fetch
            fx.REPORT_JSON = pathlib.Path(tmp) / "regional_fx.json"
            try:
                first = fx.get_regional_fx_references(conn, settings())
                second = fx.get_regional_fx_references(conn, settings())
            finally:
                fx.fetch_json = old_fetch
                fx.REPORT_JSON = old_report

        self.assertEqual(set(first), {"ZAR", "NGN"})
        self.assertEqual(set(second), {"ZAR", "NGN"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(conn.execute("select count(*) from regional_fx_snapshots where status = 'ok'").fetchone()[0], 2)

    def test_frankfurter_fallback_when_primary_errors(self) -> None:
        old_fetch = fx.fetch_json
        calls = []

        def fake_fetch(url: str, timeout: int = 10):
            calls.append(url)
            if "open.er-api.com" in url:
                raise RuntimeError("primary down")
            return {"base": "USD", "date": "2026-06-29", "rates": {"ZAR": 18.2}}

        conn = memory_conn()
        fx.fetch_json = fake_fetch
        try:
            refs = fx.get_regional_fx_references(conn, settings(), quotes={"ZAR"}, force_refresh=True)
        finally:
            fx.fetch_json = old_fetch

        self.assertEqual(refs["ZAR"]["provider"], "Frankfurter")
        self.assertEqual(len(calls), 2)

    def test_fetch_without_explicit_settings_uses_default_frontier_quotes(self) -> None:
        old_fetch = fx.fetch_json

        def fake_fetch(url: str, timeout: int = 10):
            return {
                "rates": {"AUD": 1.5, "EUR": 0.93, "GBP": 0.8},
                "time_last_update_unix": 1_700_000_000,
            }

        conn = memory_conn()
        fx.fetch_json = fake_fetch
        try:
            refs = fx.get_regional_fx_references(conn, None, quotes={"AUD", "EUR", "GBP"}, force_refresh=True)
            fallback_refs = fx.get_regional_fx_references(conn, None, force_refresh=True)
        finally:
            fx.fetch_json = old_fetch

        self.assertEqual(set(refs), {"AUD", "EUR", "GBP"})
        self.assertTrue({"AUD", "EUR", "GBP"}.issubset(set(fallback_refs)))


if __name__ == "__main__":
    unittest.main()
