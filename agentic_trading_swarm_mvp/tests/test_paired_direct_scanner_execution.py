from __future__ import annotations

import copy
import datetime as dt
import sqlite3
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import execution_engine  # noqa: E402
import okx_perp_scanner  # noqa: E402
from execution_engine import build_order_ticket, execute_order  # noqa: E402
from paired_direct_contract import (  # noqa: E402
    ACCOUNTING_CONVENTION,
    CONTRACT_VERSION,
    DECLARED_GROSS_NOTIONAL_USD,
    STRATEGY_FAMILY,
    validate_paired_direct_entry,
)
from paper_admission_queue import (  # noqa: E402
    enqueue_paper_admission_candidates,
    select_paper_admission_candidates,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


UTC = dt.timezone.utc


class PairedDirectScannerExecutionTests(unittest.TestCase):
    @staticmethod
    def settings() -> dict:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["mode"] = "paper"
        settings["allow_live_trading"] = False
        settings["risk"].update(
            {
                "paper_notional_usd": DECLARED_GROSS_NOTIONAL_USD,
                "taker_fee_bps_per_leg": 5.0,
                "slippage_bps_per_leg": 3.0,
                "max_open_paper_trades": 100,
            }
        )
        settings["market_admission"].update(
            {
                "enabled": True,
                "paper_queue_enabled": True,
                "paper_queue": {
                    "max_active": 200,
                    "max_enqueue_per_cycle": 30,
                    "max_select_per_cycle": 30,
                    "selection_lease_seconds": 900,
                    "retry_backoff_seconds": 300,
                    "max_freshness_age_seconds": 90.0,
                    "poor_cohort_min_labels": 20,
                    "poor_cohort_max_avg_pnl_bps": -8.0,
                    "poor_cohort_max_win_rate": 0.43,
                },
            }
        )
        settings.setdefault("paper_due_outcome_collection", {}).update(
            {
                "paired_max_entry_timestamp_skew_seconds": 2.0,
                "paired_max_exit_timestamp_skew_seconds": 1.0,
                "paired_notional_tolerance_fraction": 0.01,
            }
        )
        settings.setdefault("paper_expansion", {})["enabled"] = True
        settings.setdefault("paper_exploration", {})["enabled"] = False
        return settings

    @staticmethod
    def base_candidate() -> dict:
        return {
            "seen_at": dt.datetime.now(UTC).isoformat(),
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "asset_class": "crypto_linked_derivative",
            "market_surface": "okx_perpetual_swap",
            "trade_type": "perp_funding_basis",
            "direction": "short_perp_long_spot",
            "last": 101.0,
            "index_px": 7.0,
            "score": 90.0,
            "learned_score": 90.0,
            "edge_bps_estimate": 50.0,
            "gross_edge_bps_estimate": 90.0,
            "estimated_round_trip_cost_bps": 32.0,
            "liquidity_score": 0.95,
            "spread_bps": 2.0,
            "quality_score": 95.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": [],
            "route_status": "standard",
            "execution_route": {
                "route_id": "okx_derivatives_paper",
                "route_status": "standard",
                "missing_permissions": [],
            },
            "execution_feasibility": {
                "status": "standard",
                "route_status": "standard",
                "missing_requirements": [],
            },
        }

    @staticmethod
    def approved_review() -> dict:
        return {
            "decision": "approve_paper_trade",
            "learned_score": 90.0,
            "confidence": 0.9,
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 50.0,
            "route_id": "okx_derivatives_paper",
            "effective_route_id": "okx_derivatives_paper",
            "route_status": "standard",
            "feasibility_status": "standard",
            "missing_requirements": [],
            "hard_blocks": [],
        }

    @staticmethod
    def ticker(inst_id: str, event_at: dt.datetime, *, bid: float, ask: float) -> dict:
        return {
            "instId": inst_id,
            "last": str((bid + ask) / 2.0),
            "bidPx": str(bid),
            "askPx": str(ask),
            "open24h": str((bid + ask) / 2.0 - 1.0),
            "volCcy24h": "10000000",
            "ts": str(int(event_at.timestamp() * 1000)),
        }

    def paired_candidate(
        self,
        *,
        decision_time: dt.datetime | None = None,
        perp_age_seconds: float = 1.0,
        spot_age_seconds: float = 0.5,
        perp_inst_id: str = "BTC-USDT-SWAP",
        include_spot: bool = True,
    ) -> dict:
        decision_time = decision_time or dt.datetime.now(UTC)
        perp = self.ticker(
            perp_inst_id,
            decision_time - dt.timedelta(seconds=perp_age_seconds),
            bid=100.0,
            ask=100.2,
        )
        spot = (
            self.ticker(
                "BTC-USDT",
                decision_time - dt.timedelta(seconds=spot_age_seconds),
                bid=99.7,
                ask=99.9,
            )
            if include_spot
            else None
        )
        return okx_perp_scanner.apply_paired_direct_entry_contract(
            self.base_candidate(),
            perp,
            spot,
            self.settings(),
            decision_time=decision_time,
        )

    def test_scan_uses_direct_swap_bid_and_spot_ask_for_complete_contract(self) -> None:
        now = dt.datetime.now(UTC)
        perp = self.ticker("BTC-USDT-SWAP", now - dt.timedelta(seconds=1), bid=101.0, ask=101.2)
        spot = self.ticker("BTC-USDT", now - dt.timedelta(milliseconds=500), bid=99.7, ask=99.9)
        next_funding_ms = str(int((now + dt.timedelta(hours=8)).timestamp() * 1000))

        def public_response(path, params=None, timeout=12):
            del timeout
            inst_type = (params or {}).get("instType")
            if path == "/api/v5/market/tickers" and inst_type == "SWAP":
                return {"data": [perp]}
            if path == "/api/v5/market/tickers" and inst_type == "SPOT":
                return {"data": [spot]}
            payloads = {
                "/api/v5/market/index-tickers": [
                    {"instId": "BTC-USDT", "idxPx": "100.0"}
                ],
                "/api/v5/public/mark-price": [
                    {"instId": "BTC-USDT-SWAP", "markPx": "101.1", "ts": perp["ts"]}
                ],
                "/api/v5/public/open-interest": [
                    {"instId": "BTC-USDT-SWAP", "oi": "10", "oiUsd": "1000"}
                ],
                "/api/v5/public/instruments": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "baseCcy": "BTC",
                        "quoteCcy": "USDT",
                        "settleCcy": "USDT",
                        "instFamily": "BTC-USDT",
                        "state": "live",
                    }
                ],
            }
            return {"data": payloads[path]}

        funding = {
            "fundingRate": "0.0008",
            "fundingTime": perp["ts"],
            "nextFundingTime": next_funding_ms,
        }
        history = {
            "funding_history_count": 8,
            "funding_history_avg_bps": 6.0,
            "funding_history_last_bps": 7.0,
        }
        with (
            mock.patch.object(okx_perp_scanner, "fetch_json", side_effect=public_response),
            mock.patch.object(okx_perp_scanner, "get_funding", return_value=funding),
            mock.patch.object(okx_perp_scanner, "get_funding_history", return_value=history),
        ):
            batch = okx_perp_scanner.build_scan_batch(1, settings=self.settings())

        candidate = batch.candidates[0]
        validation = validate_paired_direct_entry(candidate, settings=self.settings())
        contract = candidate[CONTRACT_VERSION]
        self.assertTrue(validation["valid"], validation["reasons"])
        self.assertEqual("entry_complete", candidate["paired_direct_contract_status"])
        self.assertEqual(STRATEGY_FAMILY, contract["strategy_family"])
        self.assertEqual(ACCOUNTING_CONVENTION, contract["accounting_convention"])
        self.assertEqual(101.0, contract["entry_components"]["perp"]["price"])
        self.assertEqual(99.9, contract["entry_components"]["spot"]["price"])
        self.assertNotEqual(candidate["index_px"], contract["entry_components"]["spot"]["price"])
        self.assertEqual("OKX_SPOT", contract["entry_components"]["spot"]["venue"])
        self.assertEqual(
            DECLARED_GROSS_NOTIONAL_USD,
            sum(
                leg["notional_usd"]
                for leg in contract["entry_components"].values()
            ),
        )
        self.assertFalse(contract["funding_requirement"]["allow_estimates"])
        self.assertEqual(1, batch.metadata["paired_direct_entry_complete_count"])

        perp_event_at = okx_perp_scanner.unix_ms_to_iso(perp["ts"])
        spot_event_at = okx_perp_scanner.unix_ms_to_iso(spot["ts"])
        self.assertEqual(int(perp["ts"]), candidate["ticker_timestamp_ms"])
        self.assertEqual(perp_event_at, candidate["exchange_timestamp"])
        self.assertEqual(perp_event_at, candidate["ticker_timestamp"])
        self.assertEqual(perp_event_at, candidate["source_observed_at"])
        self.assertEqual(spot_event_at, candidate["observed_at"])
        self.assertEqual(batch.generated_at, candidate["seen_at"])
        self.assertEqual(batch.generated_at, candidate["received_at"])

        observation = batch.observations[0]
        self.assertEqual(perp_event_at, observation["exchange_timestamp"])
        self.assertEqual(perp_event_at, observation["ticker_timestamp"])
        self.assertEqual(perp_event_at, observation["source_observed_at"])
        self.assertEqual(perp_event_at, observation["observed_at"])
        self.assertEqual(batch.generated_at, observation["received_at"])

    def test_strategy_observation_never_substitutes_receipt_time_for_ticker_time(self) -> None:
        received_at = "2026-08-07T20:10:00+00:00"
        event_at = dt.datetime(2026, 8, 7, 20, 0, 0, tzinfo=UTC)
        row = self.ticker("BTC-USDT-SWAP", event_at, bid=100.0, ask=100.2)

        observation = okx_perp_scanner._strategy_observation(
            row,
            None,
            {"instId": "BTC-USDT-SWAP", "baseCcy": "BTC", "quoteCcy": "USDT"},
            received_at,
        )
        self.assertEqual(event_at.isoformat(), observation["observed_at"])
        self.assertEqual(event_at.isoformat(), observation["exchange_timestamp"])
        self.assertEqual(received_at, observation["received_at"])
        self.assertNotIn("seen_at", observation)

        missing_timestamp = okx_perp_scanner._strategy_observation(
            {**row, "ts": ""},
            None,
            None,
            received_at,
        )
        self.assertEqual(received_at, missing_timestamp["received_at"])
        self.assertNotIn("observed_at", missing_timestamp)
        self.assertNotIn("exchange_timestamp", missing_timestamp)
        self.assertNotIn("seen_at", missing_timestamp)

    def test_missing_or_misaligned_spot_is_shadow_only_and_never_uses_index(self) -> None:
        missing = self.paired_candidate(include_spot=False)
        self.assertEqual("invalid_or_incomplete", missing["paired_direct_contract_status"])
        self.assertTrue(missing["shadow_filtered"])
        self.assertFalse(missing["paper_label_eligible"])
        self.assertIsNone(missing[CONTRACT_VERSION]["entry_components"]["spot"]["price"])
        self.assertNotEqual(
            missing["index_px"],
            missing[CONTRACT_VERSION]["entry_components"]["spot"]["price"],
        )

        skewed = self.paired_candidate(perp_age_seconds=4.0, spot_age_seconds=0.5)
        self.assertEqual("invalid_or_incomplete", skewed["paired_direct_contract_status"])
        self.assertIn(
            "entry_timestamp_skew",
            skewed[CONTRACT_VERSION]["validation_reasons"],
        )

    def test_wrong_perp_response_identity_fails_closed(self) -> None:
        candidate = self.paired_candidate(perp_inst_id="ETH-USDT-SWAP")
        self.assertEqual("invalid_or_incomplete", candidate["paired_direct_contract_status"])
        self.assertIn(
            "direct_perp_ticker_identity_mismatch",
            candidate[CONTRACT_VERSION]["validation_reasons"],
        )
        self.assertFalse(candidate["paper_fill_allowed"])

    def test_oldest_leg_controls_queue_freshness_at_ninety_second_boundary(self) -> None:
        decision_time = dt.datetime(2026, 8, 7, 20, 0, 0, tzinfo=UTC)
        candidate = self.paired_candidate(
            decision_time=decision_time,
            perp_age_seconds=90.0,
            spot_age_seconds=89.0,
        )
        self.assertEqual(
            (decision_time - dt.timedelta(seconds=90)).isoformat(),
            candidate["source_observed_at"],
        )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        enqueue_paper_admission_candidates(
            conn,
            self.settings(),
            [candidate],
            now=decision_time.isoformat(),
        )
        self.assertEqual(
            1,
            len(
                select_paper_admission_candidates(
                    conn,
                    self.settings(),
                    now=decision_time.isoformat(),
                )
            ),
        )
        conn.close()

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        enqueue_paper_admission_candidates(
            conn,
            self.settings(),
            [candidate],
            now=(decision_time + dt.timedelta(milliseconds=1)).isoformat(),
        )
        self.assertEqual(
            [],
            select_paper_admission_candidates(
                conn,
                self.settings(),
                now=(decision_time + dt.timedelta(milliseconds=1)).isoformat(),
            ),
        )
        conn.close()

    def test_ticket_has_exactly_two_matched_legs_and_never_uses_top_level_last(self) -> None:
        candidate = self.paired_candidate()
        ticket = build_order_ticket(candidate, self.approved_review(), self.settings())
        self.assertEqual("ready_for_paper_execution", ticket["status"])
        self.assertEqual(DECLARED_GROSS_NOTIONAL_USD, ticket["notional_usd"])
        self.assertEqual(DECLARED_GROSS_NOTIONAL_USD, ticket["return_denominator_usd"])
        self.assertEqual(2, len(ticket["legs"]))
        self.assertEqual(["sell", "buy"], [leg["side"] for leg in ticket["legs"]])
        self.assertEqual(["OKX", "OKX_SPOT"], [leg["venue"] for leg in ticket["legs"]])
        self.assertEqual(
            ["BTC-USDT-SWAP", "BTC-USDT"],
            [leg["inst_id"] for leg in ticket["legs"]],
        )
        self.assertEqual(
            [50.0, 50.0],
            [leg["notional_usd"] for leg in ticket["legs"]],
        )
        self.assertEqual([100.0, 99.9], [leg["price"] for leg in ticket["legs"]])
        self.assertNotIn(candidate["last"], [leg["price"] for leg in ticket["legs"]])
        for leg in ticket["legs"]:
            for field in (
                "strategy_family",
                "contract_version",
                "side",
                "venue",
                "inst_id",
                "event_at",
                "price",
                "notional_usd",
                "entry_fee_bps",
                "entry_slippage_bps",
                "exit_fee_bps",
                "exit_slippage_bps",
                "quote_asset",
                "source_identity",
            ):
                self.assertIsNotNone(leg[field], field)

    def test_execution_revalidates_freshness_and_blocks_non_queue_pair(self) -> None:
        stale = self.paired_candidate()
        stale_contract = stale[CONTRACT_VERSION]
        old = dt.datetime.now(UTC) - dt.timedelta(seconds=91)
        stale_contract["entry_components"]["perp"]["event_at"] = old.isoformat()
        stale_contract["entry_components"]["spot"]["event_at"] = old.isoformat()
        ticket = build_order_ticket(stale, self.approved_review(), self.settings())
        self.assertEqual(
            "blocked_invalid_or_incomplete_paired_direct_contract",
            ticket["status"],
        )
        self.assertEqual([], ticket["legs"])
        self.assertIn("entry_components.perp.stale", ticket["paired_direct_validation_reasons"])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        non_queue = self.settings()
        non_queue["market_admission"]["paper_queue_enabled"] = False
        result = execute_order(
            conn,
            self.paired_candidate(),
            self.approved_review(),
            non_queue,
        )
        self.assertEqual("blocked_paired_direct_requires_bounded_queue", result["order"]["status"])
        self.assertEqual([], result["fills"])
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        conn.close()

    def test_second_fill_failure_rolls_back_entire_paired_bundle(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.settings()
        candidate = self.paired_candidate()
        enqueue_paper_admission_candidates(conn, settings, [candidate])
        claimed = select_paper_admission_candidates(conn, settings)[0]
        real_save_fill = execution_engine.save_execution_fill
        calls = 0

        def fail_second_fill(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced_second_fill_failure")
            return real_save_fill(*args, **kwargs)

        with mock.patch.object(
            execution_engine,
            "save_execution_fill",
            side_effect=fail_second_fill,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced_second_fill_failure"):
                execute_order(conn, claimed, self.approved_review(), settings)

        self.assertEqual(2, calls)
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from execution_fills").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trades").fetchone()[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
