from __future__ import annotations

import copy
import datetime as dt
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS
from storage import init_db
from strategy_lab import generate_strategy_lab_candidates, ingest_strategy_lab_recommendation
from strategy_reliability import (
    paper_family_quarantine_record,
    paper_source_veto_record,
    paper_source_veto_recovery_status,
)


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings() -> dict:
    result = copy.deepcopy(DEFAULT_SETTINGS)
    result["allow_live_trading"] = False
    return result


def recommendation(*, market_key: str, lab_id: str, parent: str | None = None) -> dict:
    return {
        "recommendation_id": "rec_" + lab_id,
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "title": "Test a bounded source descendant",
            "rationale": "Test only in the paper lab.",
            "market_key": market_key,
            "strategy_lab_experiment": {
                "strategy_lab_id": lab_id,
                "parent_strategy_lab_id": parent,
                "experiment_type": "market_strategy",
                "hypothesis": "A source transformation may retain paper expectancy.",
                "source_surface": "frontier_spot",
                "permitted_target_surface": ["frontier_spot"],
                "strategy_logic": {
                    "type": "candidate_filter",
                    "venues": ["OKX_SPOT"],
                    "trade_types": ["frontier_crypto_venue_map"],
                    "directions": ["long_frontier_spot"],
                },
                "data_requirements": {"paper_only": True},
                "risk_gates": {},
                "promotion_rules": {},
            },
        },
    }


class YahooProxySourceVetoTests(unittest.TestCase):
    @staticmethod
    def _recovered_settings() -> dict:
        cfg = settings()
        passing_window = {
            "sample_count": 12,
            "after_cost_expectancy_bps": 0.1,
            "freshness_pass_rate": 0.95,
            "execution_quality_pass_rate": 0.96,
        }
        cfg["strategy_lab"]["yahoo_proxy_momentum_source_veto"]["recovery_evidence"] = {
            "source_family": {"windows": [passing_window] * 3},
            "immediate_descendants": {"windows": [passing_window] * 3},
        }
        return cfg

    def test_blocks_direct_market_key_before_experiment_creation(self) -> None:
        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(
                conn,
                recommendation(
                    market_key="YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                    lab_id="new_yahoo_descendant",
                ),
            )
            count = conn.execute("select count(*) from strategy_lab_experiments").fetchone()[0]

        self.assertEqual("skipped", result[0]["action_status"])
        self.assertEqual("paper_source_family_veto", result[0]["reason"])
        self.assertEqual(0, count)

    def test_distinct_shock_reversal_lineage_does_not_relax_momentum_veto(self) -> None:
        reversal = {
            "venue": "YAHOO_PROXY",
            "inst_id": "EWZ",
            "trade_type": "global_proxy_shock_reversal",
            "direction": "short_proxy",
            "strategy_lab_source_trade_type": "global_proxy_momentum",
        }
        momentum = {**reversal, "trade_type": "global_proxy_momentum"}

        self.assertIsNone(paper_source_veto_record(reversal, settings()))
        self.assertIsNone(paper_family_quarantine_record(reversal, settings()))
        self.assertIsNotNone(paper_source_veto_record(momentum, settings()))
        self.assertIsNotNone(paper_family_quarantine_record(momentum, settings()))

    def test_ingestion_accepts_bounded_reversal_program_but_still_blocks_momentum_contract(self) -> None:
        experiment = {
            "strategy_lab_id": "global_proxy_shock_reversal_observation_v1",
            "version": 1,
            "experiment_type": "market_strategy",
            "hypothesis": "Extreme Yahoo proxy moves reverse after five-minute momentum flips.",
            "source_surface": "yahoo_proxy_global_proxy_momentum",
            "permitted_target_surface": ["proxy"],
            "strategy_logic": {
                "type": "observation_program",
                "universe": {"venues": ["YAHOO_PROXY"]},
                "calculated_features": {
                    "shock_magnitude_bps": "abs(return_60m_bps)",
                    "shock_sigma": "abs(return_60m_bps) / max(volatility_60m_bps, 10)",
                    "flip_strength_bps": "max(0, -(return_5m_bps * return_60m_bps) / max(abs(return_60m_bps), 1))",
                },
                "entry_expression": "shock_magnitude_bps >= 40 and shock_sigma >= 1.75 and flip_strength_bps >= 5 and return_5m_bps * return_60m_bps < 0",
                "invalidation_expression": "return_5m_bps * return_60m_bps >= 0",
                "long_expression": "return_60m_bps < 0 and return_5m_bps > 0",
                "short_expression": "return_60m_bps > 0 and return_5m_bps < 0",
                "edge_expression": "shock_magnitude_bps",
                "score_expression": "clip(shock_magnitude_bps, 0, 100)",
                "route_surface": "proxy",
                "output_trade_type": "global_proxy_shock_reversal",
            },
            "data_requirements": {"paper_only": True, "source_market_key": "YAHOO_PROXY"},
            "risk_gates": {},
            "promotion_rules": {},
        }
        rec = {
            "recommendation_id": "rec_shock_reversal",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "strategy_lab_experiment": experiment,
            },
        }
        with memory_db() as conn:
            created = ingest_strategy_lab_recommendation(conn, rec, settings())
            blocked = ingest_strategy_lab_recommendation(
                conn,
                recommendation(
                    market_key="YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                    lab_id="new_yahoo_descendant",
                ),
                settings(),
            )

        self.assertEqual("created", created[0]["action_status"])
        self.assertEqual("paper_source_family_veto", blocked[0]["reason"])

    def test_blocks_cross_surface_child_by_parent_lineage(self) -> None:
        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(
                conn,
                recommendation(
                    market_key="OKX_SPOT|frontier_crypto_venue_map",
                    lab_id="okx_transport_child",
                    parent="lab_yahoo_proxy_momentum_freshness_quality_gate_v1",
                ),
            )

        self.assertEqual("paper_source_family_veto", result[0]["reason"])
        self.assertEqual("strategy_lab_name_prefix", result[0]["source_veto"]["matched_on"]["type"])

    def test_excludes_direct_source_from_strategy_lab_ranking(self) -> None:
        direct = {
            "market_key": "YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional",
            "venue": "OKX_SPOT",
            "inst_id": "ABC-USDT",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "long_frontier_spot",
            "score": 99.0,
            "execution_feasibility": {"status": "standard"},
            "target_surface": "frontier_spot",
        }
        with memory_db() as conn:
            generated, report = generate_strategy_lab_candidates(conn, settings(), [direct])

        self.assertEqual([], generated)
        self.assertEqual(1, report["source_vetoed_candidate_count"])
        self.assertEqual(0, report["route_eligible_source_candidate_count"])

    def test_existing_cross_surface_experiment_is_excluded_from_ranking(self) -> None:
        rec = recommendation(
            market_key="OKX_SPOT|frontier_crypto_venue_map",
            lab_id="okx_transport_child",
            parent="lab_yahoo_proxy_momentum_freshness_quality_gate_v1",
        )
        healthy = {
            "venue": "OKX_SPOT",
            "inst_id": "ABC-USDT",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "long_frontier_spot",
            "score": 80.0,
            "liquidity_score": 0.8,
            "spread_bps": 2.0,
            "last": 10.0,
            "edge_bps_estimate": 12.0,
            "execution_feasibility": {"status": "standard"},
            "target_surface": "frontier_spot",
        }
        with memory_db() as conn:
            created = ingest_strategy_lab_recommendation(conn, rec, self._recovered_settings())
            generated, report = generate_strategy_lab_candidates(conn, settings(), [healthy])

        self.assertEqual("created", created[0]["action_status"])
        self.assertEqual([], generated)
        self.assertEqual(1, report["source_vetoed_experiment_count"])

    def test_recovered_source_still_requires_proxy_regime_and_local_frontier_confirmation(self) -> None:
        cfg = self._recovered_settings()
        rec = recommendation(
            market_key="OKX_SPOT|frontier_crypto_venue_map",
            lab_id="okx_transport_child",
            parent="lab_yahoo_proxy_momentum_freshness_quality_gate_v1",
        )
        proof = {
            "paper_only": True,
            "target_surface": "OKX_SPOT",
            "closed_count": 24,
            "expectancy_net_bps": 2.0,
            "quality_pass_rate": 0.6,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "realized_paper_outcomes": True,
            "transfer_mapping_key": (
                "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard->ABC-USDT"
            ),
            "transfer_window_count": 3,
            "positive_transfer_windows": 3,
        }
        candidate = {
            "market_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            "source_family": "yahoo_proxy",
            "signal_family": "global_proxy_momentum",
            "source_signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            "venue": "OKX_SPOT",
            "inst_id": "ABC-USDT",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "long_frontier_spot",
            "target_surface": "frontier_spot",
            "score": 80.0,
            "liquidity_score": 0.8,
            "spread_bps": 2.0,
            "local_short_horizon_trend_bps": 3.0,
            "last": 10.0,
            "edge_bps_estimate": 12.0,
            "execution_feasibility": {"status": "standard"},
            "target_surface_paper_evidence": proof,
        }
        with memory_db() as conn:
            created = ingest_strategy_lab_recommendation(conn, rec, cfg)
            blocked, blocked_report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [{**candidate, "native_yahoo_proxy_regime": {"momentum_bps": -4.0, "regime_stable": True}}],
            )
            admitted, admitted_report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [
                    {
                        **candidate,
                        "native_yahoo_proxy_regime": {
                            "momentum_bps": 8.0,
                            "regime_stable": True,
                            "regime_state": "stable_positive",
                        },
                    }
                ],
            )

        self.assertEqual("created", created[0]["action_status"])
        self.assertEqual([], blocked)
        self.assertEqual(1, blocked_report["proxy_frontier_quarantined_candidate_count"])
        self.assertEqual(
            "native_yahoo_proxy_regime_non_positive",
            blocked_report["proxy_frontier_quarantined_candidates"][0]["reason"],
        )
        self.assertEqual(1, len(admitted))
        self.assertEqual(0, admitted_report["proxy_frontier_quarantined_candidate_count"])

    def test_recovery_requires_both_scopes_and_all_sustained_windows(self) -> None:
        passing_window = {
            "sample_count": 12,
            "after_cost_expectancy_bps": 0.1,
            "freshness_pass_rate": 0.95,
            "execution_quality_pass_rate": 0.96,
        }
        cfg = self._recovered_settings()
        policy = cfg["strategy_lab"]["yahoo_proxy_momentum_source_veto"]

        recovery = paper_source_veto_recovery_status(cfg)
        veto = paper_source_veto_record(
            {"market_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy"},
            cfg,
        )

        self.assertTrue(recovery["recovered"])
        self.assertIsNone(veto)

        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(
                conn,
                recommendation(
                    market_key="YAHOO_PROXY|global_proxy_momentum|long_proxy",
                    lab_id="recovered_yahoo_descendant",
                ),
                cfg,
            )
        self.assertEqual("created", result[0]["action_status"])

        policy["recovery_evidence"]["immediate_descendants"]["windows"][1] = {
            **passing_window,
            "after_cost_expectancy_bps": -0.01,
        }
        self.assertFalse(paper_source_veto_recovery_status(cfg)["recovered"])
        self.assertIsNotNone(
            paper_source_veto_record(
                {"market_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy"},
                cfg,
            )
        )

    def test_live_context_is_not_changed_by_paper_policy(self) -> None:
        self.assertIsNone(
            paper_source_veto_record(
                {"market_key": "YAHOO_PROXY|global_proxy_momentum"},
                {"mode": "live"},
            )
        )


if __name__ == "__main__":
    unittest.main()
