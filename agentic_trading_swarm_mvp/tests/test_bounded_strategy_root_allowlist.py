from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_expansion_campaign import (  # noqa: E402
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    _bounded_strategy_root_allowlist,
)
from storage import connect  # noqa: E402
from strategy_lab import _allowlisted_experiment_ids  # noqa: E402


class BoundedStrategyRootAllowlistTests(unittest.TestCase):
    def _insert_root(
        self,
        conn,
        strategy_lab_id: str,
        *,
        source_agent: str,
        source_surface: str = "perp_funding_basis",
        target_surfaces: list[str] | None = None,
        route_status: str = "standard",
        created_at: str = "2026-08-07T00:00:00+00:00",
    ) -> None:
        surfaces = target_surfaces or ["perp_funding_basis"]
        conn.execute(
            """
            insert into strategy_lab_experiments (
                strategy_lab_id,version,parent_strategy_lab_id,experiment_type,status,
                hypothesis,strategy_logic_json,data_requirements_json,risk_gates_json,
                promotion_rules_json,source_agent,source_surface,
                permitted_target_surfaces_json,created_at,updated_at
            ) values (?,1,null,'market_strategy','active_testing',?,?,?,?,?,?,?,?,?,?)
            """,
            (
                strategy_lab_id,
                strategy_lab_id,
                json.dumps(
                    {
                        "type": "candidate_filter",
                        "trade_types": surfaces,
                        "venues": ["OKX"],
                        "asset_classes": ["crypto"],
                        "directions": ["short_perp_long_spot"],
                    }
                ),
                json.dumps({"paper_only": True, "route_status": route_status}),
                "{}",
                "{}",
                source_agent,
                source_surface,
                json.dumps(surfaces),
                created_at,
                created_at,
            ),
        )

    def test_research_allowlist_excludes_legacy_and_invalid_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, connect(
            pathlib.Path(tmp) / "radar.sqlite"
        ) as conn:
            for index in range(8):
                self._insert_root(
                    conn,
                    f"legacy_noncrypto_{index}",
                    source_agent="legacy_research",
                    created_at=f"2025-01-{index + 1:02d}T00:00:00+00:00",
                )
            self._insert_root(
                conn,
                RECOVERY_CANARY_STRATEGY_LAB_ID,
                source_agent="deterministic_recovery_bootstrap",
            )
            self._insert_root(conn, "paid_crypto_a", source_agent="paid_research_one_shot")
            self._insert_root(
                conn,
                "paid_crypto_b",
                source_agent="paid_research_one_shot",
                source_surface="frontier_crypto_venue_map",
                target_surfaces=["frontier_crypto_venue_map"],
            )
            self._insert_root(
                conn,
                "paid_bad_route",
                source_agent="paid_research_one_shot",
                route_status="unresolved",
            )
            self._insert_root(
                conn,
                "paid_bad_surface",
                source_agent="paid_research_one_shot",
                source_surface="global_proxy_momentum",
                target_surfaces=["global_proxy_momentum"],
            )
            conn.commit()

            allowed_roots = _bounded_strategy_root_allowlist(
                conn,
                phase="research",
                configured=[],
                max_roots=6,
            )
            self.assertEqual(
                [RECOVERY_CANARY_STRATEGY_LAB_ID, "paid_crypto_a", "paid_crypto_b"],
                allowed_roots,
            )
            allowed_ids, roots = _allowlisted_experiment_ids(conn, allowed_roots, 6)
            self.assertEqual(set(allowed_roots), allowed_ids)
            self.assertNotIn("legacy_noncrypto_0", allowed_ids)
            self.assertEqual("legacy_noncrypto_0", roots["legacy_noncrypto_0"])

    def test_explicit_empty_recovery_allowlist_authorizes_no_legacy_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, connect(
            pathlib.Path(tmp) / "radar.sqlite"
        ) as conn:
            self._insert_root(
                conn,
                "legacy_active_root",
                source_agent="legacy_research",
            )
            conn.commit()

            allowed_ids, roots = _allowlisted_experiment_ids(
                conn,
                [],
                6,
                include_descendants=False,
                empty_allowlist_means_none=True,
            )

            self.assertEqual(set(), allowed_ids)
            self.assertEqual("legacy_active_root", roots["legacy_active_root"])


if __name__ == "__main__":
    unittest.main()
