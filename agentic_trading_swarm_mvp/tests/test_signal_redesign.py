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

import signal_redesign
import okx_signal_research
import self_improvement
import storage
from scan_batch import ScanBatch, merge_observations
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["learning"]["horizon_minutes"] = [60]
    cfg["learning"]["max_outcome_delay_seconds"] = 300
    return cfg


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def insert_trade(conn: sqlite3.Connection, opened_at: str, inst_id: str = "TEST") -> int:
    candidate = {
        "seen_at": opened_at,
        "venue": "TEST",
        "inst_id": inst_id,
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": 60.0,
        "last": 100.0,
        "thesis": "test",
        "execution_feasibility": {"status": "standard"},
    }
    review = {"learned_score": 60.0, "route_status": "standard"}
    trade_id = storage.open_paper_trade(conn, candidate, review)
    conn.execute("update paper_trades set opened_at = ? where id = ?", (opened_at, trade_id))
    conn.commit()
    return trade_id


class ReliableOutcomeTests(unittest.TestCase):
    def test_complete_observation_prices_trade_outside_ranked_candidates(self) -> None:
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        batch = ScanBatch(
            source="test",
            candidates=[],
            observations=[
                {
                    "inst_id": "OUTSIDE_TOP",
                    "last": 101.0,
                    "observed_at": observed_at,
                    "price_source": "complete_universe",
                }
            ],
        )

        merged = merge_observations([batch])

        self.assertIn("OUTSIDE_TOP", merged)
        self.assertEqual(merged["OUTSIDE_TOP"]["price_source"], "complete_universe")

    def test_valid_outcome_closes_trade_at_target_horizon(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        opened_at = (now - dt.timedelta(minutes=61)).isoformat()
        trade_id = insert_trade(conn, opened_at)
        observations = {
            "TEST": {
                "inst_id": "TEST",
                "last": 101.0,
                "observed_at": now.isoformat(),
                "price_source": "complete_universe",
            }
        }

        recorded = storage.record_due_horizon_outcomes(conn, observations, settings())
        closed = storage.close_due_trades(conn, observations, 60, settings())

        self.assertEqual(recorded[0]["measurement_status"], "valid")
        self.assertEqual(closed[0]["measurement_status"], "valid")
        row = conn.execute(
            "select status, close_measurement_status, pnl_bps from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_measurement_status"], "valid")
        self.assertIsNotNone(row["pnl_bps"])

    def test_missing_outcome_expires_trade_without_fake_pnl(self) -> None:
        conn = memory_conn()
        opened_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=66)).isoformat()
        trade_id = insert_trade(conn, opened_at, inst_id="MISSING")

        recorded = storage.record_due_horizon_outcomes(conn, {}, settings())
        storage.close_due_trades(conn, {}, 60, settings())

        self.assertEqual(recorded[0]["measurement_status"], "missing")
        row = conn.execute(
            "select status, pnl_bps, close_measurement_status from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["status"], "expired_unpriced")
        self.assertIsNone(row["pnl_bps"])
        self.assertEqual(row["close_measurement_status"], "missing")


class SignalVariantTests(unittest.TestCase):
    def test_variant_validation_rejects_arbitrary_fields(self) -> None:
        config = copy.deepcopy(signal_redesign.DEFAULT_VARIANTS[0]["config"])
        config["python_code"] = "import os"

        with self.assertRaises(ValueError):
            signal_redesign.validate_variant_config(config)

    def test_malformed_llm_variant_is_skipped_without_artifact(self) -> None:
        conn = memory_conn()
        result = self_improvement._execute_signal_variant(
            conn,
            {
                "recommendation_id": "bad-variant",
                "payload": {
                    "action": "propose_signal_variant",
                    "title": "Unsafe variant",
                    "variant_config": {"python_code": "import os"},
                },
            },
        )

        self.assertEqual(result[0]["skip_reason"], "variant_validation_failed")
        self.assertEqual(conn.execute("select count(*) from signal_variants").fetchone()[0], 0)

    def test_initial_variants_are_immutable_and_single_active(self) -> None:
        conn = memory_conn()

        signal_redesign.ensure_initial_variants(conn)
        signal_redesign.ensure_initial_variants(conn)
        rows = signal_redesign.load_variants(conn)

        self.assertEqual(len(rows), 16)
        self.assertEqual(sum(row["status"] == "active" for row in rows), 1)
        self.assertEqual(
            next(row["variant_id"] for row in rows if row["status"] == "active"),
            "frontier_v1_incumbent",
        )
        self.assertIn("frontier_v5_short_route_quality", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v11_regional_fx_depth_probe", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v12_okx_spot_survivor", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v13_gate_mexc_short_probe", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v14_bybit_spot_long_expansion", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v15_bybit_quality_decay_expand", {row["variant_id"] for row in rows})
        self.assertIn("frontier_v16_kucoin_long_repair_probe", {row["variant_id"] for row in rows})

    def test_systemic_variant_config_accepts_bounded_fields(self) -> None:
        config = copy.deepcopy(
            next(row for row in signal_redesign.DEFAULT_VARIANTS if row["variant_id"] == "frontier_v5_short_route_quality")["config"]
        )

        validated = signal_redesign.validate_variant_config(config)

        self.assertEqual(validated["allowed_directions"], ["short_frontier_spot"])
        self.assertTrue(validated["require_public_order_book"])
        self.assertFalse(validated["allow_regional_quotes"])

    def test_expansion_variants_are_bounded_shadow_probes(self) -> None:
        by_id = {row["variant_id"]: row for row in signal_redesign.DEFAULT_VARIANTS}

        regional = signal_redesign.validate_variant_config(by_id["frontier_v11_regional_fx_depth_probe"]["config"])
        self.assertEqual(by_id["frontier_v11_regional_fx_depth_probe"]["status"], "shadow")
        self.assertTrue(regional["allow_regional_quotes"])
        self.assertIn("external_fx_reference", regional["allowed_quote_normalization_statuses"])
        self.assertGreaterEqual(regional["min_quality_score"], 70.0)

        okx = signal_redesign.validate_variant_config(by_id["frontier_v12_okx_spot_survivor"]["config"])
        self.assertEqual(okx["allowed_venues"], ["OKX_SPOT"])
        self.assertTrue(okx["require_public_order_book"])

        gate_mexc = signal_redesign.validate_variant_config(by_id["frontier_v13_gate_mexc_short_probe"]["config"])
        self.assertEqual(gate_mexc["allowed_venues"], ["GATE", "MEXC"])
        self.assertEqual(gate_mexc["allowed_directions"], ["short_frontier_spot"])
        self.assertEqual(gate_mexc["direction_mode"], "short_only")

        bybit = signal_redesign.validate_variant_config(by_id["frontier_v14_bybit_spot_long_expansion"]["config"])
        self.assertEqual(bybit["allowed_venues"], ["BYBIT_SPOT"])
        self.assertEqual(bybit["allowed_directions"], ["long_frontier_spot"])
        self.assertEqual(bybit["allowed_route_statuses"], ["standard"])
        self.assertEqual(bybit["direction_mode"], "long_only")

        bybit_decay = signal_redesign.validate_variant_config(by_id["frontier_v15_bybit_quality_decay_expand"]["config"])
        self.assertEqual(by_id["frontier_v15_bybit_quality_decay_expand"]["status"], "shadow")
        self.assertEqual(bybit_decay["allowed_venues"], ["BYBIT_SPOT"])
        self.assertEqual(bybit_decay["allowed_directions"], ["long_frontier_spot"])
        self.assertGreaterEqual(bybit_decay["min_quality_score"], 75.0)

        kucoin = signal_redesign.validate_variant_config(by_id["frontier_v16_kucoin_long_repair_probe"]["config"])
        self.assertEqual(by_id["frontier_v16_kucoin_long_repair_probe"]["status"], "shadow")
        self.assertEqual(kucoin["allowed_venues"], ["KUCOIN"])
        self.assertEqual(kucoin["allowed_directions"], ["long_frontier_spot"])
        self.assertGreaterEqual(kucoin["min_quality_score"], 80.0)
        self.assertGreaterEqual(kucoin["min_source_venue_count"], 4)

    def test_paired_trials_are_recorded_for_incumbent_and_challenger(self) -> None:
        conn = memory_conn()
        signal_redesign.ensure_initial_variants(conn)
        variants = signal_redesign.load_variants(conn)
        candidate = {
            "seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "venue": "GATE",
            "inst_id": "GATE:ABC_USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 60.0,
            "last": 101.0,
            "candidate_reject_reason": None,
            "execution_feasibility": {"status": "conditional"},
        }
        by_variant = {
            "frontier_v1_incumbent": [{**candidate, "signal_variant_id": "frontier_v1_incumbent"}],
            "frontier_v3_quality_short": [{**candidate, "signal_variant_id": "frontier_v3_quality_short"}],
        }

        activity = signal_redesign.record_variant_trials(
            conn,
            variants,
            by_variant,
            settings(),
            "scan-1",
        )

        self.assertEqual(activity["created"], 2)
        pairs = conn.execute(
            "select count(distinct pair_key) as n, count(*) as rows from signal_trials"
        ).fetchone()
        self.assertEqual(pairs["n"], 1)
        self.assertEqual(pairs["rows"], 2)

    def test_shadow_trial_caps_and_low_quality_skips_are_enforced(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["signal_redesign"].update(
            {
                "max_trials_per_loop": 3,
                "max_trials_per_variant_per_loop": 2,
                "max_trials_per_venue_per_loop": 2,
                "min_shadow_trial_quality_score": 35.0,
            }
        )
        variants = [
            {"variant_id": "frontier_v1_incumbent", "status": "active"},
            {"variant_id": "frontier_v3_quality_short", "status": "shadow"},
        ]
        good = {
            "seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "venue": "GATE",
            "inst_id": "GATE:GOOD_USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 60.0,
            "last": 101.0,
            "quality_score": 70.0,
            "candidate_reject_reason": None,
            "execution_feasibility": {"status": "conditional"},
        }
        low_quality = {
            **good,
            "inst_id": "GATE:LOW_USDT",
            "quality_score": 10.0,
        }
        more = {
            **good,
            "inst_id": "GATE:MORE_USDT",
        }

        activity = signal_redesign.record_variant_trials(
            conn,
            variants,
            {
                "frontier_v1_incumbent": [good, more],
                "frontier_v3_quality_short": [good, low_quality, more],
            },
            cfg,
            "scan-capped",
        )

        self.assertLessEqual(activity["created"], 3)
        self.assertIn("low_quality_shadow", activity["skipped_by_reason"])
        self.assertEqual(activity["caps"]["max_trials_per_variant_per_loop"], 2)

    def test_challenger_promotes_after_two_consecutive_passes(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["signal_redesign"].update(
            {
                "min_paired_trials": 3,
                "min_observation_hours": 0,
                "min_valid_label_rate": 0.95,
                "min_opportunity_coverage": 0.3,
                "consecutive_passes_to_promote": 2,
            }
        )
        signal_redesign.ensure_initial_variants(conn)
        self._insert_paired_outcomes(
            conn,
            "frontier_v1_incumbent",
            "frontier_v3_quality_short",
            [-10.0, -8.0, -6.0],
            [20.0, 22.0, 24.0],
        )

        first = signal_redesign.evaluate_variants(conn, cfg)
        second = signal_redesign.evaluate_variants(conn, cfg)

        first_v3 = next(row for row in first if row["variant_id"] == "frontier_v3_quality_short")
        self.assertEqual(first_v3["consecutive_passes"], 1)
        active = conn.execute(
            "select variant_id from signal_variants where status = 'active'"
        ).fetchone()["variant_id"]
        self.assertEqual(active, "frontier_v3_quality_short")
        self.assertTrue(any(row["decision"] == "promoted" for row in second))

    def test_promoted_variant_reverts_to_fallback_after_regression(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["signal_redesign"].update(
            {
                "min_paired_trials": 3,
                "min_observation_hours": 0,
                "min_valid_label_rate": 0.95,
                "min_opportunity_coverage": 0.3,
            }
        )
        signal_redesign.ensure_initial_variants(conn)
        conn.execute("update signal_variants set status = 'shadow'")
        conn.execute(
            """
            update signal_variants
            set status = 'active', fallback_variant_id = 'frontier_v1_incumbent'
            where variant_id = 'frontier_v3_quality_short'
            """
        )
        conn.commit()
        self._insert_paired_outcomes(
            conn,
            "frontier_v1_incumbent",
            "frontier_v3_quality_short",
            [20.0, 22.0, 24.0],
            [-20.0, -22.0, -24.0],
        )

        results = signal_redesign.evaluate_variants(conn, cfg)

        active = conn.execute(
            "select variant_id from signal_variants where status = 'active'"
        ).fetchone()["variant_id"]
        self.assertEqual(active, "frontier_v1_incumbent")
        self.assertTrue(any(row["status"] == "reverted" for row in results))

    def _insert_paired_outcomes(
        self,
        conn: sqlite3.Connection,
        incumbent_id: str,
        challenger_id: str,
        incumbent_pnls: list[float],
        challenger_pnls: list[float],
    ) -> None:
        created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        for index, (incumbent_pnl, challenger_pnl) in enumerate(
            zip(incumbent_pnls, challenger_pnls)
        ):
            pair_key = f"pair-{index}"
            for variant_id, pnl in (
                (incumbent_id, incumbent_pnl),
                (challenger_id, challenger_pnl),
            ):
                candidate = {
                    "signal_variant_id": variant_id,
                    "direction": "short_frontier_spot",
                }
                cur = conn.execute(
                    """
                    insert into signal_trials (
                        created_at, scan_id, trial_bucket, pair_key, variant_id,
                        signal_family, signal_key, inst_id, venue, direction,
                        entry_price, candidate_json, eligible
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        (created + dt.timedelta(minutes=index)).isoformat(),
                        f"scan-{index}",
                        f"bucket-{index}",
                        pair_key,
                        variant_id,
                        signal_redesign.SIGNAL_FAMILY,
                        "GATE|frontier_crypto_venue_map|short_frontier_spot|conditional",
                        f"GATE:TEST{index}_USDT",
                        "GATE",
                        "short_frontier_spot",
                        100.0,
                        json.dumps(candidate),
                    ),
                )
                conn.execute(
                    """
                    insert into signal_trial_outcomes (
                        trial_id, horizon_minutes, target_at, observed_at,
                        delay_seconds, measurement_status, price, pnl_bps,
                        price_source
                    ) values (?, 60, ?, ?, 30, 'valid', 99, ?, 'test')
                    """,
                    (
                        cur.lastrowid,
                        created.isoformat(),
                        created.isoformat(),
                        pnl,
                    ),
                )
        conn.commit()


class OkxSignalResearchTests(unittest.TestCase):
    def test_initial_okx_variants_are_immutable_and_single_active(self) -> None:
        conn = memory_conn()

        okx_signal_research.ensure_initial_variants(conn)
        okx_signal_research.ensure_initial_variants(conn)
        rows = okx_signal_research.load_variants(conn)

        self.assertEqual(len(rows), 9)
        self.assertEqual(sum(row["status"] == "active" for row in rows), 1)
        self.assertEqual(
            next(row["variant_id"] for row in rows if row["status"] == "active"),
            "okx_v1_incumbent",
        )

    def test_funding_alignment_preserves_aligned_capture_and_rejects_misaligned(self) -> None:
        variant = next(row for row in okx_signal_research.DEFAULT_VARIANTS if row["variant_id"] == "okx_v2_funding_alignment")
        aligned = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=5.0,
            basis_bps=10.0,
            route_status="standard",
        )
        misaligned = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=-5.0,
            basis_bps=10.0,
            route_status="standard",
            inst_id="OKX:ETH-USDT-SWAP",
        )

        rows = okx_signal_research.build_variant_candidates([aligned, misaligned], settings(), variant)
        by_inst = {row["inst_id"]: row for row in rows}

        self.assertEqual(by_inst["OKX:BTC-USDT-SWAP"]["direction"], "funding_capture_short_perp")
        self.assertIsNone(by_inst["OKX:BTC-USDT-SWAP"].get("candidate_reject_reason"))
        self.assertEqual(by_inst["OKX:ETH-USDT-SWAP"]["direction"], "watch_only")
        self.assertEqual(by_inst["OKX:ETH-USDT-SWAP"]["candidate_reject_reason"], "funding_and_basis_not_aligned")

    def test_basis_regime_gate_requires_cooling_regime(self) -> None:
        variant = next(row for row in okx_signal_research.DEFAULT_VARIANTS if row["variant_id"] == "okx_v3_basis_regime_gate")
        confirmed = self._okx_candidate(
            "basis_mean_reversion_short_perp",
            funding_bps=1.0,
            basis_bps=60.0,
            change_24h_pct=6.0,
            route_status="standard",
        )
        reinforcing = self._okx_candidate(
            "basis_mean_reversion_short_perp",
            funding_bps=8.0,
            basis_bps=60.0,
            change_24h_pct=6.0,
            route_status="standard",
            inst_id="OKX:SOL-USDT-SWAP",
        )

        rows = okx_signal_research.build_variant_candidates([confirmed, reinforcing], settings(), variant)
        by_inst = {row["inst_id"]: row for row in rows}

        self.assertEqual(by_inst["OKX:BTC-USDT-SWAP"]["direction"], "basis_mean_reversion_short_perp")
        self.assertEqual(by_inst["OKX:SOL-USDT-SWAP"]["direction"], "watch_only")
        self.assertEqual(by_inst["OKX:SOL-USDT-SWAP"]["candidate_reject_reason"], "basis_regime_not_confirmed")

    def test_reverse_basis_recovery_stays_shadow_and_capped(self) -> None:
        variant = next(row for row in okx_signal_research.DEFAULT_VARIANTS if row["variant_id"] == "okx_v4_reverse_basis_recovery")
        candidate = self._okx_candidate(
            "long_perp_short_spot",
            funding_bps=-3.0,
            basis_bps=-25.0,
            route_status="conditional",
            score=80.0,
        )

        rows = okx_signal_research.build_variant_candidates([candidate], settings(), variant)

        self.assertEqual(rows[0]["direction"], "long_perp_short_spot")
        self.assertEqual(rows[0]["score"], 45.0)
        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertTrue(rows[0]["okx_recovery_shadow_only"])

    def test_okx_carry_economics_models_net_edge_and_borrow_uncertainty(self) -> None:
        candidate = self._okx_candidate(
            "short_perp_long_spot",
            funding_bps=20.0,
            basis_bps=30.0,
            route_status="standard",
        )

        enriched = okx_signal_research.add_carry_economics(candidate, settings())

        self.assertEqual(enriched["expected_funding_bps_to_next"], 20.0)
        self.assertEqual(enriched["basis_alignment_edge_bps"], 30.0)
        self.assertGreater(enriched["net_carry_edge_bps"], 0.0)
        self.assertEqual(enriched["carry_alignment_status"], "carry_aligned_positive")
        self.assertEqual(enriched["borrow_cost_status"], "not_required")

        reverse = self._okx_candidate(
            "long_perp_short_spot",
            funding_bps=-20.0,
            basis_bps=-30.0,
            route_status="conditional",
            inst_id="OKX:ETH-USDT-SWAP",
        )
        reverse_enriched = okx_signal_research.add_carry_economics(reverse, settings())

        self.assertEqual(reverse_enriched["borrow_cost_status"], "unknown")
        self.assertEqual(reverse_enriched["carry_alignment_status"], "borrow_unknown")
        self.assertIsNone(reverse_enriched["borrow_cost_bps"])

    def test_net_carry_variant_requires_positive_cost_adjusted_edge(self) -> None:
        variant = next(row for row in okx_signal_research.DEFAULT_VARIANTS if row["variant_id"] == "okx_v7_net_carry_positive")
        positive = self._okx_candidate(
            "short_perp_long_spot",
            funding_bps=20.0,
            basis_bps=30.0,
            route_status="standard",
        )
        eroded = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=1.0,
            basis_bps=0.0,
            route_status="standard",
            inst_id="OKX:SOL-USDT-SWAP",
        )

        rows = okx_signal_research.build_variant_candidates([positive, eroded], settings(), variant)
        by_inst = {row["inst_id"]: row for row in rows}

        self.assertEqual(by_inst["OKX:BTC-USDT-SWAP"]["direction"], "short_perp_long_spot")
        self.assertTrue(by_inst["OKX:BTC-USDT-SWAP"]["okx_net_carry_variant"])
        self.assertEqual(by_inst["OKX:SOL-USDT-SWAP"]["direction"], "watch_only")
        self.assertIn(
            by_inst["OKX:SOL-USDT-SWAP"]["candidate_reject_reason"],
            {"net_carry_edge_below_variant_minimum", "carry_economics_not_aligned"},
        )

    def test_basis_carry_disagreement_variant_is_shadow_only(self) -> None:
        variant = next(
            row for row in okx_signal_research.DEFAULT_VARIANTS if row["variant_id"] == "okx_v9_basis_carry_disagree_shadow"
        )
        candidate = self._okx_candidate(
            "basis_mean_reversion_short_perp",
            funding_bps=-4.0,
            basis_bps=45.0,
            route_status="standard",
            score=80.0,
        )

        rows = okx_signal_research.build_variant_candidates([candidate], settings(), variant)

        self.assertEqual(rows[0]["direction"], "basis_mean_reversion_short_perp")
        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertTrue(rows[0]["okx_basis_carry_shadow_only"])
        self.assertEqual(rows[0]["score"], 35.0)

    def test_okx_trials_are_recorded_under_okx_signal_family(self) -> None:
        conn = memory_conn()
        okx_signal_research.ensure_initial_variants(conn)
        variants = okx_signal_research.load_variants(conn)
        active = next(row for row in variants if row["variant_id"] == "okx_v1_incumbent")
        candidate = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=5.0,
            basis_bps=10.0,
            route_status="standard",
        )

        activity = okx_signal_research.record_variant_trials(
            conn,
            [active],
            {"okx_v1_incumbent": [candidate]},
            settings(),
            "okx-scan-1",
        )
        row = conn.execute("select signal_family, variant_id from signal_trials").fetchone()

        self.assertEqual(activity["created"], 1)
        self.assertEqual(row["signal_family"], okx_signal_research.SIGNAL_FAMILY)
        self.assertEqual(row["variant_id"], "okx_v1_incumbent")

    def test_okx_paper_trade_receives_valid_reliable_outcome(self) -> None:
        conn = memory_conn()
        okx_signal_research.ensure_initial_variants(conn)
        opened_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)).isoformat()
        candidate = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=5.0,
            basis_bps=10.0,
            route_status="standard",
        )
        candidate["seen_at"] = opened_at
        review = {"learned_score": 70.0, "route_status": "standard"}
        trade_id = storage.open_paper_trade(conn, candidate, review)
        conn.execute("update paper_trades set opened_at = ? where id = ?", (opened_at, trade_id))
        conn.commit()
        observations = {
            candidate["inst_id"]: {
                "inst_id": candidate["inst_id"],
                "venue": "OKX",
                "trade_type": "perp_funding_basis",
                "last": 99.0,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "price_source": "OKX public REST market tickers",
            }
        }

        recorded = storage.record_due_horizon_outcomes(conn, observations, settings())

        self.assertEqual(recorded[0]["measurement_status"], "valid")
        self.assertEqual(recorded[0]["price_source"], "OKX public REST market tickers")

    def test_okx_signal_trial_receives_valid_label_from_complete_observation(self) -> None:
        conn = memory_conn()
        okx_signal_research.ensure_initial_variants(conn)
        variants = okx_signal_research.load_variants(conn)
        active = next(row for row in variants if row["variant_id"] == "okx_v1_incumbent")
        candidate = self._okx_candidate(
            "funding_capture_short_perp",
            funding_bps=5.0,
            basis_bps=10.0,
            route_status="standard",
        )
        okx_signal_research.record_variant_trials(
            conn,
            [active],
            {"okx_v1_incumbent": [candidate]},
            settings(),
            "okx-scan-2",
        )
        opened_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)).isoformat()
        conn.execute("update signal_trials set created_at = ?", (opened_at,))
        conn.commit()
        observations = {
            candidate["inst_id"]: {
                "inst_id": candidate["inst_id"],
                "venue": "OKX",
                "trade_type": "perp_funding_basis",
                "last": 99.0,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "price_source": "OKX public REST market tickers",
            }
        }

        recorded = okx_signal_research.record_trial_outcomes(conn, observations, settings())

        self.assertEqual(recorded[0]["measurement_status"], "valid")
        row = conn.execute(
            "select measurement_status from signal_trial_outcomes where horizon_minutes = 60"
        ).fetchone()
        self.assertEqual(row["measurement_status"], "valid")

    def _okx_candidate(
        self,
        direction: str,
        *,
        funding_bps: float,
        basis_bps: float,
        route_status: str,
        change_24h_pct: float = 5.0,
        score: float = 70.0,
        inst_id: str = "OKX:BTC-USDT-SWAP",
    ) -> dict:
        symbol = inst_id.split(":", 1)[1]
        return {
            "seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "venue": "OKX",
            "inst_id": inst_id,
            "symbol": symbol,
            "direction": direction,
            "trade_type": "perp_funding_basis",
            "score": score,
            "last": 100.0,
            "basis_bps": basis_bps,
            "funding_bps": funding_bps,
            "spread_bps": 2.0,
            "liquidity_score": 0.7,
            "change_24h_pct": change_24h_pct,
            "execution_feasibility": {"status": route_status},
            "candidate_reject_reason": None,
            "promotion_eligible": True,
        }


if __name__ == "__main__":
    unittest.main()
