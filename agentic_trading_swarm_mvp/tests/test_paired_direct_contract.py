from __future__ import annotations

import copy
import datetime as dt
import json
import math
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paired_direct_contract import (  # noqa: E402
    ACCOUNTING_CONVENTION,
    CONTRACT_VERSION,
    DECLARED_GROSS_NOTIONAL_USD,
    STRATEGY_FAMILY,
    calculate_paired_direct_outcome,
    validate_paired_direct_entry,
    validate_paired_funding_coverage,
)
from storage import (  # noqa: E402
    init_db,
    open_paper_trade,
    performance_summary,
    reliable_paper_label_eligibility_for_trade_row,
)
from strategy_lab import _experiment_outcomes  # noqa: E402


T0 = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)
TARGET = T0 + dt.timedelta(minutes=15)


def source(name: str, inst_id: str, event_at: dt.datetime) -> dict:
    return {
        "name": "OKX public REST",
        "endpoint": "/api/v5/market/ticker",
        "parser": "okx_ticker_event_v1",
        "event_id": f"{name}:{inst_id}:{event_at.isoformat()}",
    }


def candidate(*, perp_price: float = 100.0, spot_price: float = 100.0) -> dict:
    spot_at = T0 + dt.timedelta(seconds=1)
    return {
        "direction": "short_perp_long_spot",
        CONTRACT_VERSION: {
            "contract_version": CONTRACT_VERSION,
            "strategy_family": STRATEGY_FAMILY,
            "status": "entry_complete",
            "accounting_convention": ACCOUNTING_CONVENTION,
            "quote_asset": "USDT",
            "max_entry_timestamp_skew_seconds": 2.0,
            "notional_match_tolerance_fraction": 0.01,
            "declared_gross_notional_usd": DECLARED_GROSS_NOTIONAL_USD,
            "return_denominator_usd": DECLARED_GROSS_NOTIONAL_USD,
            "entry_components": {
                "perp": {
                    "side": "short",
                    "venue": "OKX",
                    "inst_id": "BTC-USDT-SWAP",
                    "market_surface": "perp",
                    "quote_asset": "USDT",
                    "event_at": T0.isoformat(),
                    "price": perp_price,
                    "notional_usd": 50.0,
                    "entry_fee_bps": 1.0,
                    "entry_slippage_bps": 2.0,
                    "exit_fee_bps": 1.0,
                    "exit_slippage_bps": 2.0,
                    "source": source("perp", "BTC-USDT-SWAP", T0),
                },
                "spot": {
                    "side": "long",
                    "venue": "OKX_SPOT",
                    "inst_id": "BTC-USDT",
                    "market_surface": "spot",
                    "quote_asset": "USDT",
                    "event_at": spot_at.isoformat(),
                    "price": spot_price,
                    "notional_usd": 50.0,
                    "entry_fee_bps": 1.0,
                    "entry_slippage_bps": 2.0,
                    "exit_fee_bps": 1.0,
                    "exit_slippage_bps": 2.0,
                    "source": source("spot", "BTC-USDT", spot_at),
                },
            },
            "funding_requirement": {
                "required": True,
                "venue": "OKX",
                "inst_id": "BTC-USDT-SWAP",
                "source_endpoint": "/api/v5/public/funding-rate-history",
                "source_parser": "okx_realized_funding_history",
                "allow_estimates": False,
            },
        },
    }


def exits(*, perp_price: float = 90.0, spot_price: float = 110.0) -> dict:
    event_at = TARGET + dt.timedelta(seconds=60)

    def row(venue: str, inst_id: str, price: float) -> dict:
        return {
            "observation_id": f"{venue}:{inst_id}:{event_at.isoformat()}",
            "venue": venue,
            "inst_id": inst_id,
            "market_surface": "perp" if venue == "OKX" else "spot",
            "candle_open_at": (event_at - dt.timedelta(seconds=60)).isoformat(),
            "event_at": event_at.isoformat(),
            "received_at": (event_at + dt.timedelta(minutes=20)).isoformat(),
            "price": price,
            "source_kind": "exchange_candle_1m_close",
            "source_parser": "okx_1m_candles",
            "source_endpoint": "/api/v5/market/history-candles",
            "source_event_id": f"{venue}:{inst_id}:{event_at.isoformat()}",
            "is_closed": True,
            "is_partial": False,
        }

    return {
        "perp": row("OKX", "BTC-USDT-SWAP", perp_price),
        "spot": row("OKX_SPOT", "BTC-USDT", spot_price),
    }


def funding(*, realized_rate: float | None = 0.001) -> dict:
    exit_at = TARGET + dt.timedelta(seconds=60)
    events = []
    if realized_rate is not None:
        events.append(
            {
                "event_at": (T0 + dt.timedelta(minutes=10)).isoformat(),
                "realized_rate": realized_rate,
                "source_event_id": "OKX:BTC-USDT-SWAP:funding:1",
                "method": "current_period",
                "formula_type": "withRateCap",
                "estimated": False,
            }
        )
    return {
        "batch_id": "funding-coverage-test-1",
        "coverage_status": "complete",
        "complete_from": T0.isoformat(),
        "complete_through": exit_at.isoformat(),
        "allow_estimates": False,
        "source": {
            "name": "OKX public REST",
            "endpoint": "/api/v5/public/funding-rate-history",
            "parser": "okx_realized_funding_history",
            "inst_id": "BTC-USDT-SWAP",
        },
        "query": {
            "request_url": "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=400",
            "requested_from": T0.isoformat(),
            "requested_through": exit_at.isoformat(),
            "received_at": (exit_at + dt.timedelta(seconds=1)).isoformat(),
            "request_succeeded": True,
            "http_status": 200,
            "page_count": 1,
            "pagination_complete": True,
            "range_complete": True,
            "query_id": "okx-funding-query-1",
            "payload_sha256": "a" * 64,
        },
        "events": events,
    }


class PairedDirectContractTests(unittest.TestCase):
    def test_entry_requires_exact_two_leg_identity_notional_and_sources(self) -> None:
        valid = validate_paired_direct_entry(candidate())
        self.assertTrue(valid["valid"], valid["reasons"])

        cases = []
        missing_spot = candidate()
        missing_spot[CONTRACT_VERSION]["entry_components"].pop("spot")
        cases.append(missing_spot)
        wrong_venue = candidate()
        wrong_venue[CONTRACT_VERSION]["entry_components"]["spot"]["venue"] = "OKX"
        cases.append(wrong_venue)
        skewed = candidate()
        skewed[CONTRACT_VERSION]["entry_components"]["spot"]["event_at"] = (
            T0 + dt.timedelta(seconds=3)
        ).isoformat()
        cases.append(skewed)
        unmatched = candidate()
        unmatched[CONTRACT_VERSION]["entry_components"]["spot"]["notional_usd"] = 45.0
        cases.append(unmatched)
        bad_denominator = candidate()
        bad_denominator[CONTRACT_VERSION]["return_denominator_usd"] = 100.0
        bad_denominator[CONTRACT_VERSION]["declared_gross_notional_usd"] = 90.0
        cases.append(bad_denominator)
        for leg_notional in (10.0, 500.0):
            wrong_campaign_notional = candidate()
            wrong_campaign_notional[CONTRACT_VERSION]["entry_components"]["perp"][
                "notional_usd"
            ] = leg_notional
            wrong_campaign_notional[CONTRACT_VERSION]["entry_components"]["spot"][
                "notional_usd"
            ] = leg_notional
            wrong_campaign_notional[CONTRACT_VERSION][
                "declared_gross_notional_usd"
            ] = leg_notional * 2.0
            wrong_campaign_notional[CONTRACT_VERSION][
                "return_denominator_usd"
            ] = leg_notional * 2.0
            cases.append(wrong_campaign_notional)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                self.assertFalse(validate_paired_direct_entry(invalid)["valid"])

    def test_funding_accepts_complete_empty_interval_but_never_estimates(self) -> None:
        contract = candidate()[CONTRACT_VERSION]
        no_events = validate_paired_funding_coverage(
            contract,
            funding(realized_rate=None),
            TARGET,
        )
        self.assertTrue(no_events["valid"], no_events["reasons"])

        fabricated = funding(realized_rate=None)
        fabricated.pop("query")
        rejected_empty = validate_paired_funding_coverage(
            contract,
            fabricated,
            TARGET,
        )
        self.assertFalse(rejected_empty["valid"])
        self.assertIn("query.request_succeeded", rejected_empty["reasons"])

        estimated = funding()
        estimated["events"][0].pop("realized_rate")
        estimated["events"][0]["funding_rate"] = 0.001
        estimated["events"][0]["estimated"] = True
        rejected = validate_paired_funding_coverage(contract, estimated, TARGET)
        self.assertFalse(rejected["valid"])
        self.assertIn("events[0].realized_rate", rejected["reasons"])

        missing_estimate_marker = funding()
        missing_estimate_marker["events"][0].pop("estimated")
        marker_rejected = validate_paired_funding_coverage(
            contract,
            missing_estimate_marker,
            TARGET,
        )
        self.assertIn("events[0].estimated", marker_rejected["reasons"])

    def test_composite_outcome_names_both_candles_and_realized_funding(self) -> None:
        result = calculate_paired_direct_outcome(candidate(), exits(), funding(), TARGET)
        self.assertTrue(result["valid"], result["reasons"])
        self.assertAlmostEqual(999.0, result["pnl_bps"], places=9)
        context = result["context"]
        self.assertEqual(ACCOUNTING_CONVENTION, context["accounting_convention"])
        self.assertTrue(context["exit_components"]["perp"]["observation_id"])
        self.assertTrue(context["exit_components"]["spot"]["observation_id"])
        accounting = context["accounting"]
        self.assertAlmostEqual(0.05, accounting["realized_funding_usd"], places=12)
        self.assertAlmostEqual(
            accounting["pnl_bps"],
            accounting["reconciliation_sum_bps"],
            places=12,
        )
        self.assertAlmostEqual(0.0, accounting["reconciliation_error_bps"], places=12)

    def test_funding_coverage_must_extend_through_later_actual_exit(self) -> None:
        staggered_exits = exits()
        later_exit = TARGET + dt.timedelta(seconds=90)
        staggered_exits["spot"]["event_at"] = later_exit.isoformat()
        staggered_exits["spot"]["candle_open_at"] = (
            later_exit - dt.timedelta(seconds=60)
        ).isoformat()
        staggered_exits["perp"]["event_at"] = later_exit.isoformat()
        staggered_exits["perp"]["candle_open_at"] = (
            later_exit - dt.timedelta(seconds=60)
        ).isoformat()

        insufficient = funding(realized_rate=None)
        insufficient["complete_through"] = (
            TARGET + dt.timedelta(seconds=60)
        ).isoformat()
        insufficient["query"]["requested_through"] = insufficient[
            "complete_through"
        ]

        result = calculate_paired_direct_outcome(
            candidate(), staggered_exits, insufficient, TARGET
        )
        self.assertFalse(result["valid"])
        self.assertIn("complete_through", result["reasons"])
        self.assertIn("query.requested_through", result["reasons"])

    def test_flat_market_loses_exactly_declared_round_trip_costs_once(self) -> None:
        result = calculate_paired_direct_outcome(
            candidate(),
            exits(perp_price=100.0, spot_price=100.0),
            funding(realized_rate=None),
            TARGET,
        )
        self.assertTrue(result["valid"], result["reasons"])
        self.assertAlmostEqual(-6.0, result["pnl_bps"], places=12)
        accounting = result["context"]["accounting"]
        self.assertAlmostEqual(0.0, accounting["perp_gross_pnl_usd"], places=12)
        self.assertAlmostEqual(0.0, accounting["spot_gross_pnl_usd"], places=12)
        self.assertAlmostEqual(0.06, accounting["entry_cost_usd"] + accounting["exit_cost_usd"], places=12)

    def test_single_partial_or_pretarget_exit_never_forms_direct_label(self) -> None:
        cases = []
        one_leg = exits()
        one_leg.pop("spot")
        cases.append(one_leg)
        partial = exits()
        partial["spot"]["is_partial"] = True
        cases.append(partial)
        pretarget = exits()
        pretarget["perp"]["event_at"] = (TARGET - dt.timedelta(seconds=1)).isoformat()
        cases.append(pretarget)
        for invalid_exits in cases:
            with self.subTest(exits=invalid_exits):
                result = calculate_paired_direct_outcome(
                    candidate(), invalid_exits, funding(), TARGET
                )
                self.assertFalse(result["valid"])
                self.assertTrue(result["reasons"])
                self.assertNotIn("pnl_bps", result)

    def test_nonfinite_values_fail_closed(self) -> None:
        invalid = candidate()
        invalid[CONTRACT_VERSION]["entry_components"]["perp"]["price"] = math.nan
        self.assertFalse(validate_paired_direct_entry(invalid)["valid"])

    def test_shared_reliability_boundary_rejects_legacy_and_tampered_paired_rows(self) -> None:
        direct_candidate = candidate()
        exit_rows = exits(perp_price=100.0, spot_price=100.0)
        outcome = calculate_paired_direct_outcome(
            direct_candidate,
            exit_rows,
            funding(realized_rate=None),
            TARGET,
        )
        self.assertTrue(outcome["valid"], outcome["reasons"])
        close_context = {
            "signal_stats_scope": "direct",
            "paper_outcome_measurement_contract": CONTRACT_VERSION,
            "paired_direct_v1_outcome": outcome["context"],
            "paper_price_observations": {
                name: {"observation_id": row["observation_id"]}
                for name, row in exit_rows.items()
            },
        }
        valid_row = {
            "status": "closed",
            "direction": "short_perp_long_spot",
            "candidate_json": json.dumps(direct_candidate),
            "context_json": json.dumps(close_context),
            "close_measurement_status": "valid",
            "pnl_bps": round(float(outcome["pnl_bps"]), 3),
        }
        eligibility = reliable_paper_label_eligibility_for_trade_row(valid_row)
        self.assertTrue(eligibility["paper_label_eligible"], eligibility)

        tampered_row = dict(valid_row, pnl_bps=float(valid_row["pnl_bps"]) + 1.0)
        tampered = reliable_paper_label_eligibility_for_trade_row(tampered_row)
        self.assertFalse(tampered["paper_label_eligible"])
        self.assertEqual("short_perp_only_proxy", tampered["paper_label_exclusion_reason"])
        self.assertIn("row.pnl_bps_mismatch", tampered["paired_direct_outcome_reasons"])

        legacy_row = {
            "status": "closed",
            "direction": "short_perp_long_spot",
            "candidate_json": json.dumps(
                {"direction": "short_perp_long_spot", "paper_label_eligible": True}
            ),
            "context_json": json.dumps({"paper_label_eligible": True}),
            "pnl_bps": 5.0,
        }
        legacy = reliable_paper_label_eligibility_for_trade_row(legacy_row)
        self.assertFalse(legacy["paper_label_eligible"])
        self.assertEqual("short_perp_only_proxy", legacy["paper_label_exclusion_reason"])

    def test_strategy_lab_consumes_flat_paired_net_label_without_second_cost_charge(self) -> None:
        strategy_id = "paired_direct_flat_cost_lab"
        direct_candidate = candidate()
        direct_candidate.update(
            {
                "venue": "OKX",
                "inst_id": "BTC-USDT-SWAP",
                "trade_type": "perp_funding_basis",
                "score": 75.0,
                "last": 100.0,
                "strategy_lab_id": strategy_id,
                "strategy_lab_version": 1,
                "execution_feasibility": {"status": "standard"},
                "paper_context_cost_gate": {
                    "applicable": True,
                    "context_cost_floor_bps": 40.0,
                    "inputs": {"route_status": "standard"},
                },
            }
        )
        exit_rows = exits(perp_price=100.0, spot_price=100.0)
        paired_result = calculate_paired_direct_outcome(
            direct_candidate,
            exit_rows,
            funding(realized_rate=None),
            TARGET,
        )
        self.assertTrue(paired_result["valid"], paired_result["reasons"])
        self.assertAlmostEqual(-6.0, paired_result["pnl_bps"], places=12)
        label_context = {
            "signal_stats_scope": "direct",
            "paper_outcome_measurement_contract": CONTRACT_VERSION,
            "paired_direct_v1_outcome": paired_result["context"],
            "paper_price_observations": {
                name: {"observation_id": row["observation_id"]}
                for name, row in exit_rows.items()
            },
        }
        observed_at = str(paired_result["observed_at"])
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            init_db(conn)
            trade_id = open_paper_trade(
                conn,
                direct_candidate,
                {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                },
                settings={"scanner": {"hold_minutes": 15}},
            )
            conn.execute(
                """
                update paper_trades
                set status='closed', closed_at=?, pnl_bps=?, context_json=?,
                    close_measurement_status='valid', close_observed_at=?,
                    target_close_at=?, close_delay_seconds=60
                where id=?
                """,
                (
                    observed_at,
                    round(float(paired_result["pnl_bps"]), 3),
                    json.dumps(label_context, sort_keys=True),
                    observed_at,
                    TARGET.isoformat(),
                    trade_id,
                ),
            )
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,
                    context_json,target_at,observed_at,delay_seconds,
                    measurement_status,price_source
                ) values (?,?,?,?,?,?,?,?,?,'valid',?)
                """,
                (
                    trade_id,
                    15,
                    observed_at,
                    100.0,
                    round(float(paired_result["pnl_bps"]), 3),
                    json.dumps(label_context, sort_keys=True),
                    TARGET.isoformat(),
                    observed_at,
                    60.0,
                    CONTRACT_VERSION,
                ),
            )
            conn.commit()
            lab_outcomes = _experiment_outcomes(conn, strategy_id, 15, settings={})

            legacy_candidate = {
                key: value
                for key, value in direct_candidate.items()
                if key != CONTRACT_VERSION
            }
            legacy_candidate.update(
                {
                    "inst_id": "ETH-USDT-SWAP",
                    "last": 100.0,
                    "paper_label_eligible": True,
                }
            )
            legacy_trade_id = open_paper_trade(
                conn,
                legacy_candidate,
                {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                },
                settings={"scanner": {"hold_minutes": 15}},
            )
            conn.execute(
                """
                update paper_trades
                set status='closed', closed_at=?, pnl_bps=25.0,
                    close_measurement_status='valid'
                where id=?
                """,
                (observed_at, legacy_trade_id),
            )
            conn.commit()
            aggregate = performance_summary(conn)
        finally:
            conn.close()

        self.assertEqual(1, lab_outcomes["valid_count"], lab_outcomes)
        self.assertAlmostEqual(-6.0, lab_outcomes["metrics"]["avg_pnl_bps"], places=12)
        self.assertEqual(0, lab_outcomes["realized_cost_backfill"]["applied_count"])
        self.assertEqual(1, aggregate["closed"], aggregate)
        self.assertAlmostEqual(-6.0, aggregate["avg_pnl_bps"], places=12)


if __name__ == "__main__":
    unittest.main()
