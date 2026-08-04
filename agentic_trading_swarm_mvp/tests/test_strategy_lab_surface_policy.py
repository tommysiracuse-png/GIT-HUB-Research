from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from radar_loop import _select_runtime_strategy_lab_candidates
from settings import DEFAULT_SETTINGS
from storage import init_db, open_paper_trade
from strategy_lab import (
    _experiment_outcomes,
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
    strategy_lab_summary,
)


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def recommendation(*, include_surfaces: bool = True) -> dict:
    experiment = {
        "strategy_lab_id": "surface_policy_lab",
        "experiment_type": "market_strategy",
        "hypothesis": "Frontier spot continuation is tested only on explicitly permitted spot targets.",
        "strategy_logic": {
            "type": "candidate_filter",
            "trade_types": ["frontier_crypto_venue_map"],
            "directions": ["long_frontier_spot"],
        },
        "data_requirements": {"paper_only": True},
        "risk_gates": {},
        "promotion_rules": {},
    }
    if include_surfaces:
        experiment.update(
            {
                "source_surface": "frontier_spot",
                "permitted_target_surface": ["frontier_spot"],
            }
        )
    return {
        "recommendation_id": "rec_surface_policy",
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "title": "Bound frontier spot continuation",
            "rationale": "Keep surface-specific assumptions local.",
            "strategy_lab_experiment": experiment,
        },
    }


def candidate(inst_id: str, surface: str | None, score: float = 70.0) -> dict:
    row = {
        "venue": "OKX_SPOT",
        "inst_id": inst_id,
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": score,
        "liquidity_score": 0.8,
        "spread_bps": 2.0,
        "last": 10.0,
        "edge_bps_estimate": 15.0,
        "execution_feasibility": {"status": "standard"},
    }
    if surface is not None:
        row["target_surface"] = surface
    return row


class StrategyLabSurfacePolicyTests(unittest.TestCase):
    def settings(self) -> dict:
        result = copy.deepcopy(DEFAULT_SETTINGS)
        result["allow_live_trading"] = False
        return result

    def test_missing_idea_metadata_is_persisted_as_reviewable_quarantine(self) -> None:
        with connection() as conn:
            result = ingest_strategy_lab_recommendation(
                conn, recommendation(include_surfaces=False)
            )
            row = conn.execute(
                "select status, surface_policy_json from strategy_lab_experiments"
            ).fetchone()
            summary = strategy_lab_summary(conn)

        self.assertEqual("quarantined", result[0]["action_status"])
        self.assertEqual("quarantined_surface_policy", row["status"])
        self.assertIn("source_surface", row["surface_policy_json"])
        self.assertEqual(1, len(summary["surface_quarantine_review"]))

    def test_yahoo_proxy_cross_surface_contract_is_quarantined_at_ingestion(self) -> None:
        rec = recommendation()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = (
            "gate_yahoo_momentum_to_fresh_tight_high_quality_proxies_3342a7f1"
        )
        experiment["source_surface"] = "YAHOO_PROXY"
        experiment["permitted_target_surface"] = ["frontier_spot"]

        with connection() as conn:
            result = ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                "select status, surface_policy_json from strategy_lab_experiments"
            ).fetchone()

        policy = json.loads(row["surface_policy_json"])
        self.assertEqual("quarantined", result[0]["action_status"])
        self.assertEqual("quarantined_surface_policy", row["status"])
        self.assertIsNotNone(result[0]["source_veto"])
        self.assertEqual("yahoo_proxy_same_surface_required", policy["reason"])
        self.assertEqual(
            ["frontier_spot"],
            policy["yahoo_proxy_same_surface_review"]["blocked_target_surfaces"],
        )

    def test_yahoo_proxy_same_surface_contract_remains_eligible(self) -> None:
        rec = recommendation()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["source_surface"] = "YAHOO_PROXY"
        experiment["permitted_target_surface"] = ["YAHOO_PROXY"]

        with connection() as conn:
            result = ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                "select status, surface_policy_json from strategy_lab_experiments"
            ).fetchone()

        policy = json.loads(row["surface_policy_json"])
        self.assertEqual("created", result[0]["action_status"])
        self.assertNotEqual("quarantined_surface_policy", row["status"])
        self.assertTrue(policy["eligible"])

    def test_yahoo_proxy_explicit_cross_surface_request_cannot_hide_in_allowed_list(self) -> None:
        rec = recommendation()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["source_surface"] = "YAHOO_PROXY"
        experiment["permitted_target_surface"] = ["YAHOO_PROXY"]
        experiment["target_surface"] = "OKX_SPOT"

        with connection() as conn:
            result = ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                "select status, surface_policy_json from strategy_lab_experiments"
            ).fetchone()

        policy = json.loads(row["surface_policy_json"])
        self.assertEqual("quarantined", result[0]["action_status"])
        self.assertEqual("quarantined_surface_policy", row["status"])
        self.assertEqual("okx_spot", policy["requested_target_surface"])

    def test_runtime_ranker_rechecks_yahoo_proxy_cached_verdict(self) -> None:
        cross_surface = {
            "strategy_lab_id": "yahoo-cross-surface",
            "strategy_lab_logic_type": "candidate_filter",
            "inst_id": "BTC-USDT",
            "score": 99.0,
            "source_surface": "YAHOO_PROXY",
            "target_surface": "frontier_spot",
            "permitted_target_surface": ["frontier_spot"],
            "strategy_lab_surface_policy": {"eligible": True},
        }
        same_surface = {
            **cross_surface,
            "strategy_lab_id": "yahoo-same-surface",
            "inst_id": "YAHOO_PROXY:EWZ",
            "target_surface": "YAHOO_PROXY",
            "permitted_target_surface": ["YAHOO_PROXY"],
        }

        selected, summary = _select_runtime_strategy_lab_candidates(
            [cross_surface, same_surface],
            {"strategy_lab": {"runtime_candidate_filters_enabled": True}},
        )

        self.assertEqual(["YAHOO_PROXY:EWZ"], [row["inst_id"] for row in selected])
        self.assertEqual(1, summary["accepted_count"])

    def test_preexisting_active_yahoo_cross_surface_artifact_is_quarantined(self) -> None:
        rec = recommendation()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["source_surface"] = "YAHOO_PROXY"
        experiment["permitted_target_surface"] = ["YAHOO_PROXY"]

        with connection() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            conn.execute(
                """
                update strategy_lab_experiments
                set status = 'active_testing', compile_status = 'compiled',
                    permitted_target_surfaces_json = '["frontier_spot"]'
                """
            )
            conn.commit()
            generated, _report = generate_strategy_lab_candidates(
                conn,
                self.settings(),
                [candidate("BTC-USDT", "frontier_spot")],
            )
            row = conn.execute(
                "select status, compile_status from strategy_lab_experiments"
            ).fetchone()

        self.assertEqual([], generated)
        self.assertEqual("quarantined_surface_policy", row["status"])
        self.assertEqual("surface_quarantined", row["compile_status"])

    def test_incompatible_and_missing_targets_are_excluded_from_construction(self) -> None:
        with connection() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation())
            generated, report = generate_strategy_lab_candidates(
                conn,
                self.settings(),
                [candidate("PERP", "perp_funding_basis"), candidate("UNKNOWN", None)],
            )

        self.assertEqual([], generated)
        diagnostic = report["contract_compilation"]["diagnostics"]["surface_policy_lab"]
        self.assertEqual("no_surface_compatible_runtime_evidence", diagnostic["reason"])
        self.assertEqual(2, diagnostic["surface_quarantined_candidate_count"])
        self.assertEqual(2, report["surface_quarantined_construction_count"])

    def test_ranking_uses_only_explicitly_compatible_targets(self) -> None:
        with connection() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation())
            generated, report = generate_strategy_lab_candidates(
                conn,
                self.settings(),
                [
                    candidate("INCOMPATIBLE_HIGH", "perp_funding_basis", 99.0),
                    candidate("COMPATIBLE", "frontier_spot", 70.0),
                ],
            )

        self.assertEqual(["COMPATIBLE"], [row["inst_id"] for row in generated])
        self.assertTrue(generated[0]["strategy_lab_surface_policy"]["eligible"])
        self.assertEqual(1, report["surface_quarantined_application_count"])

    def test_runtime_ranker_denies_missing_surface_verdict(self) -> None:
        base = {
            "strategy_lab_id": "lab",
            "strategy_lab_logic_type": "candidate_filter",
            "score": 99.0,
        }
        selected, summary = _select_runtime_strategy_lab_candidates(
            [base, {**base, "inst_id": "OK", "strategy_lab_surface_policy": {"eligible": True}}],
            {"strategy_lab": {"runtime_candidate_filters_enabled": True}},
        )

        self.assertEqual(["OK"], [row["inst_id"] for row in selected])
        self.assertEqual(1, summary["accepted_count"])

    def test_backtest_attribution_quarantines_cross_surface_labels(self) -> None:
        experiment = {
            "source_surface": "frontier_spot",
            "permitted_target_surface": ["frontier_spot"],
        }
        review = {
            "learned_score": 75.0,
            "decision": "approve_paper_trade",
            "route_status": "standard",
            "hard_blocks": [],
        }
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with connection() as conn:
            for surface in ("frontier_spot", "perp_funding_basis"):
                row = candidate(surface.upper(), surface)
                row.update(
                    {
                        "strategy_lab_id": "surface_policy_lab",
                        "strategy_lab_version": 1,
                    }
                )
                trade_id = open_paper_trade(conn, row, review, settings=self.settings())
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 11, 10, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, now, now, now),
                )
            conn.commit()
            outcomes = _experiment_outcomes(
                conn, "surface_policy_lab", 60, experiment
            )

        self.assertEqual(1, outcomes["metrics"]["count"])
        self.assertEqual(1, outcomes["surface_quarantined_count"])
        self.assertEqual(
            {"valid": 1, "surface_quarantined": 1},
            outcomes["label_status_counts"],
        )


if __name__ == "__main__":
    unittest.main()
