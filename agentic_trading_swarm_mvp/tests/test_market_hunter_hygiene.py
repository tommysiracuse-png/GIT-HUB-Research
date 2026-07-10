from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_hunter
import storage


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


class HunterDirectiveHygieneTests(unittest.TestCase):
    def test_stale_implemented_directive_is_closed_but_new_work_remains_open(self) -> None:
        conn = memory_conn()
        conn.execute(
            """
            insert into adapter_specs (
                created_at, source_recommendation_id, market_key, priority,
                title, status, spec_json, evidence_json
            ) values (?, ?, ?, ?, ?, ?, '{}', '{}')
            """,
            (
                storage.utc_now(),
                "manual_spec:154",
                "OKX|perp_funding_basis",
                90,
                "OKX basis research",
                "implemented_okx_basis_signal_research",
            ),
        )
        storage.add_hunter_directive(
            conn,
            "OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
            "demote_or_filter",
            95,
            "OKX perp funding basis signal is still failing after old paper outcomes.",
            {"avg_pnl_bps": -190.0},
        )
        storage.add_hunter_directive(
            conn,
            "new_market_surface",
            "observe",
            60,
            "New unrelated market surface needs observations.",
            {"source": "fresh"},
        )

        old_json = market_hunter.HYGIENE_JSON
        old_md = market_hunter.HYGIENE_MD
        with tempfile.TemporaryDirectory() as tmp:
            market_hunter.HYGIENE_JSON = pathlib.Path(tmp) / "hygiene.json"
            market_hunter.HYGIENE_MD = pathlib.Path(tmp) / "hygiene.md"
            try:
                report = market_hunter.clean_stale_hunter_directives(conn, {})
            finally:
                market_hunter.HYGIENE_JSON = old_json
                market_hunter.HYGIENE_MD = old_md

        open_items = storage.open_hunter_directives(conn)

        self.assertEqual(report["closed_count"], 1)
        self.assertEqual(len(open_items), 1)
        self.assertEqual(open_items[0]["market_key"], "new_market_surface")
        closed_status = conn.execute(
            """
            select status from market_hunter_directives
            where market_key like 'OKX|perp_funding_basis%'
            """
        ).fetchone()["status"]
        self.assertEqual(closed_status, "superseded_by_implemented_okx_basis_signal_research")

    def test_okx_evidence_does_not_misclassify_non_okx_directives(self) -> None:
        conn = memory_conn()
        conn.execute(
            """
            insert into adapter_specs (
                created_at, source_recommendation_id, market_key, priority,
                title, status, spec_json, evidence_json
            ) values (?, ?, ?, ?, ?, ?, '{}', '{}')
            """,
            (
                storage.utc_now(),
                "manual_spec:154",
                "OKX|perp_funding_basis",
                90,
                "OKX basis research",
                "implemented_okx_basis_signal_research",
            ),
        )
        storage.add_hunter_directive(
            conn,
            "BINANCE_US|frontier_crypto_venue_map|short_frontier_spot|conditional",
            "llm_research_directive",
            95,
            "Frontier short signal needs fresh analysis.",
            {"related_context": "OKX carry is working but this is a separate frontier venue."},
        )

        old_json = market_hunter.HYGIENE_JSON
        old_md = market_hunter.HYGIENE_MD
        with tempfile.TemporaryDirectory() as tmp:
            market_hunter.HYGIENE_JSON = pathlib.Path(tmp) / "hygiene.json"
            market_hunter.HYGIENE_MD = pathlib.Path(tmp) / "hygiene.md"
            try:
                report = market_hunter.clean_stale_hunter_directives(conn, {})
            finally:
                market_hunter.HYGIENE_JSON = old_json
                market_hunter.HYGIENE_MD = old_md

        self.assertEqual(report["closed_count"], 0)
        self.assertEqual(len(storage.open_hunter_directives(conn)), 1)


if __name__ == "__main__":
    unittest.main()
