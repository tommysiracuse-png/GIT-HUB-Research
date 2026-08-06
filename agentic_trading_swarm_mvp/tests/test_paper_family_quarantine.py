from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import strategy_reliability
from paper_order_router import apply_frontier_paper_guard, frontier_shadow_filter_reason
from strategy_reliability import apply_strategy_reliability, paper_family_quarantine_record


class PaperFamilyQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_json = strategy_reliability.REPORT_JSON
        self.old_md = strategy_reliability.REPORT_MD
        strategy_reliability.REPORT_JSON = pathlib.Path(self.tmp.name) / "reliability.json"
        strategy_reliability.REPORT_MD = pathlib.Path(self.tmp.name) / "reliability.md"

    def tearDown(self) -> None:
        strategy_reliability.REPORT_JSON = self.old_json
        strategy_reliability.REPORT_MD = self.old_md
        self.tmp.cleanup()

    @staticmethod
    def _candidate(**overrides: object) -> dict:
        candidate = {
            "venue": "YAHOO_PROXY",
            "inst_id": "YAHOO_PROXY:EWZ",
            "direction": "long_proxy",
            "trade_type": "global_proxy_momentum",
            "score": 72.0,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        candidate.update(overrides)
        return candidate

    def test_matches_structured_current_family_metadata(self) -> None:
        candidate = self._candidate(
            venue="OTHER",
            trade_type="other",
            family_metadata={
                "source_family": "Yahoo Proxy",
                "feature_family": "global-proxy-momentum",
            },
        )

        record = paper_family_quarantine_record(candidate, config={"mode": "paper"})

        self.assertEqual(record["family_key"], "YAHOO_PROXY|global_proxy_momentum")
        self.assertEqual(record["matched_on"]["type"], "family_metadata")
        self.assertFalse(record["paper_score_eligible"])
        self.assertEqual(record["paper_score_multiplier"], 0.0)

    def test_matches_direction_descendant_by_family_key_prefix(self) -> None:
        candidate = self._candidate(
            venue="OTHER",
            trade_type="other",
            signal_key="YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional|v4",
        )

        record = paper_family_quarantine_record(candidate)

        self.assertEqual(record["matched_on"]["type"], "family_key_prefix")
        self.assertFalse(record["paper_rank_eligible"])

    def test_matches_current_cycle_strategy_lab_descendant_name_prefix(self) -> None:
        candidate = self._candidate(
            venue="OKX_SPOT",
            trade_type="frontier_crypto_venue_map",
            strategy_lab_id="red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0_asia_child_v2",
        )

        record = paper_family_quarantine_record(candidate)

        self.assertEqual(record["matched_on"]["type"], "strategy_lab_name_prefix")
        self.assertEqual(
            record["matched_on"]["prefix"],
            "red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
        )

    def test_releases_direct_family_when_one_leg_recovers(self) -> None:
        candidate = self._candidate(
            latest_family_paper={
                "long_proxy_standard": {"closed_count": 30, "avg_pnl_bps": 1.25, "win_rate": 0.52},
                "short_proxy_conditional": {"closed_count": 28, "avg_pnl_bps": -3.0, "win_rate": 0.41},
            }
        )

        self.assertIsNone(paper_family_quarantine_record(candidate, config={"mode": "paper"}))

    def test_releases_descendant_family_when_recovery_windows_pass(self) -> None:
        passing_window = {
            "sample_count": 12,
            "after_cost_expectancy_bps": 0.1,
            "freshness_pass_rate": 0.95,
            "execution_quality_pass_rate": 0.96,
        }
        candidate = self._candidate(
            venue="OKX_SPOT",
            trade_type="frontier_crypto_venue_map",
            strategy_lab_id="red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0_asia_child_v2",
        )

        record = paper_family_quarantine_record(
            candidate,
            config={
                "mode": "paper",
                "strategy_lab": {
                    "yahoo_proxy_momentum_source_veto": {
                        "recovery_evidence": {
                            "source_family": {"windows": [passing_window] * 3},
                            "immediate_descendants": {"windows": [passing_window] * 3},
                        }
                    }
                },
            },
        )

        self.assertIsNone(record)

    def test_does_not_quarantine_unrelated_yahoo_or_momentum_family(self) -> None:
        yahoo_mean_reversion = self._candidate(trade_type="proxy_mean_reversion")
        non_yahoo_momentum = self._candidate(venue="DIRECT_JSE")
        name_collision = self._candidate(
            venue="DIRECT_JSE",
            trade_type="proxy_mean_reversion",
            strategy_lab_id="new_red_team_yahoo_proxy_momentum_sanity_check_c6d14fc0",
        )

        self.assertIsNone(paper_family_quarantine_record(yahoo_mean_reversion))
        self.assertIsNone(paper_family_quarantine_record(non_yahoo_momentum))
        self.assertIsNone(paper_family_quarantine_record(name_collision))

    def test_is_disabled_for_live_context_or_explicit_paper_flag(self) -> None:
        candidate = self._candidate()

        self.assertIsNone(paper_family_quarantine_record(candidate, config={"mode": "live"}))
        self.assertIsNone(paper_family_quarantine_record(candidate, config=False))

    def test_scoring_gate_clamps_score_and_preserves_observability(self) -> None:
        quarantined = self._candidate(score=99.0)
        healthy = self._candidate(
            venue="DIRECT_JSE",
            inst_id="DIRECT_JSE:NPN",
            trade_type="global_proxy_momentum",
            score=40.0,
        )

        rows, report = apply_strategy_reliability([quarantined, healthy], settings={"mode": "paper"})
        by_inst = {row["inst_id"]: row for row in rows}
        blocked = by_inst["YAHOO_PROXY:EWZ"]

        self.assertEqual(rows[0]["inst_id"], "DIRECT_JSE:NPN")
        self.assertEqual(blocked["pre_quarantine_score"], 99.0)
        self.assertEqual(blocked["score"], 0.0)
        self.assertFalse(blocked["paper_score_eligible"])
        self.assertFalse(blocked["promotion_eligible"])
        self.assertEqual(blocked["strategy_reliability_allocation_multiplier"], 0.0)
        self.assertIn("paper_strategy_quarantine", blocked)
        self.assertEqual(report["summary"]["family_quarantine_count"], 1)

    def test_hard_gate_survives_general_reliability_feature_disable(self) -> None:
        candidate = self._candidate(score=88.0)

        rows, report = apply_strategy_reliability(
            [candidate],
            settings={"mode": "paper", "strategy_reliability": {"enabled": False}},
        )

        self.assertFalse(report["enabled"])
        self.assertTrue(report["paper_family_quarantine_enabled"])
        self.assertEqual(rows[0]["score"], 0.0)
        self.assertFalse(rows[0]["paper_rank_eligible"])

    def test_router_shadow_filters_quarantined_candidate(self) -> None:
        candidate = self._candidate(paper_filled=True, status="paper_filled")

        reason = frontier_shadow_filter_reason(candidate, config={"mode": "paper"})
        guarded = apply_frontier_paper_guard(candidate, config={"mode": "paper"})

        self.assertEqual(reason["guard"], "paper_strategy_family_quarantine")
        self.assertFalse(reason["paper_fill_allowed"])
        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_filled"])
        self.assertEqual(guarded["status"], "shadow_filtered")

    def test_router_prefers_freshness_gate_over_family_quarantine_when_yahoo_telemetry_is_present(self) -> None:
        candidate = self._candidate(
            direction="long_proxy",
            source_quote_timestamp="2026-08-06T14:00:00+00:00",
            source_session_status="closed",
            source_session_open=False,
            source_quote_age_seconds=1140.0,
            last_trade_timestamp="2026-08-06T14:00:00+00:00",
            last_trade_age_seconds=1140.0,
            pre_entry_tick_returns_bps=[-18.0, -10.0, 5.0, -6.0],
            proxy_reuse_gate={
                "quote_age_seconds": 1140.0,
                "source_session_status": "closed",
                "reasons": ["opening_gap_without_live_followthrough"],
            },
            seen_at="2026-08-06T14:19:00+00:00",
            paper_filled=True,
            status="paper_filled",
        )

        reason = frontier_shadow_filter_reason(candidate, config={"mode": "paper"})
        guarded = apply_frontier_paper_guard(candidate, config={"mode": "paper"})

        self.assertEqual("paper_yahoo_proxy_freshness_shadow_gate", reason["guard"])
        self.assertEqual("shadow_only", guarded["paper_action"])
        self.assertTrue(guarded["paper_observation_only"])
        self.assertEqual("synthetic_research", guarded["signal_stats_scope"])


if __name__ == "__main__":
    unittest.main()
