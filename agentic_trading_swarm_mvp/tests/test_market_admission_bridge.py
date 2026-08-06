from __future__ import annotations

import copy
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_admission_bridge
from settings import DEFAULT_SETTINGS
from storage import init_db


def state(stage: str, **overrides):
    value = {
        "admission_key": f"key-{stage}",
        "venue": "BITKUB",
        "inst_id": "BITKUB:BTC_THB",
        "market_surface": "regional_spot",
        "strategy_lineage": "adapter_observation",
        "current_stage": stage,
        "highest_stage": stage,
        "blocker_code": None,
        "session_status": "open",
        "details": {"quality_status": "verified", "route_status": "standard", "valid_labels": 0},
    }
    value.update(overrides)
    return value


class MarketAdmissionBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_json = market_admission_bridge.REPORT_JSON
        self.old_md = market_admission_bridge.REPORT_MD
        market_admission_bridge.REPORT_JSON = pathlib.Path(self.temp.name) / "bridge.json"
        market_admission_bridge.REPORT_MD = pathlib.Path(self.temp.name) / "bridge.md"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)

    def tearDown(self) -> None:
        self.conn.close()
        market_admission_bridge.REPORT_JSON = self.old_json
        market_admission_bridge.REPORT_MD = self.old_md
        self.temp.cleanup()

    def test_priceable_market_creates_one_canonical_enrichment_directive(self) -> None:
        report = {"states": [state("priceable", blocker_code="quality_unverified")]}
        first = market_admission_bridge.run_market_admission_bridge(self.conn, self.settings, report)
        second = market_admission_bridge.run_market_admission_bridge(self.conn, self.settings, report)
        self.assertEqual(1, first["summary"]["actions_created"])
        self.assertEqual(1, second["summary"]["duplicates_suppressed"])
        self.assertEqual(1, self.conn.execute("select count(*) from market_hunter_directives").fetchone()[0])

    def test_quality_verified_observation_creates_strategy_lab_discovery(self) -> None:
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [state("quality_verified")]}
        )
        self.assertEqual(1, result["summary"]["actions_created"])
        row = self.conn.execute("select * from strategy_lab_experiments").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("admission_discovery_", row["strategy_lab_id"])

    def test_eex_priceable_reported_spot_creates_canonical_synthetic_research_program(self) -> None:
        eex_spot = state(
            "priceable",
            venue="EEX",
            inst_id="EEX:EUAA:SPOT:691200",
            market_surface="eex_eu_ets_secondary_spot_trades",
            session_status="continuous",
            blocker_code="quality_unverified",
            details={"quality_status": "official_reported_trade", "route_status": "unknown"},
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [eex_spot]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, risk_gates_json from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_eex_spot_program", result["actions"][0]["action"])
        self.assertEqual("eex_eu_ets_secondary_spot_reported_trade_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual(
            "reported_trade_valid",
            logic["calculated_features"]["reported_trade_validation_signal"],
        )
        self.assertNotIn("reported_trade_valid", logic["entry_expression"])
        self.assertEqual("proxy", logic["route_surface"])
        self.assertTrue(json.loads(row["risk_gates_json"])["synthetic_research_only"])

    def test_adx_quality_verified_companion_quotes_create_canonical_program(self) -> None:
        adx_state = state(
            "quality_verified",
            venue="ADX",
            inst_id="ADX:FUTURES:SSF_ADNOC_GAS",
            market_surface="adx_equity_and_index_futures_contract_catalog",
            session_status="unknown",
            details={"quality_status": "verified_proxy", "route_status": "unknown"},
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [adx_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_adx_derivatives_program", result["actions"][0]["action"])
        self.assertEqual("adx_derivatives_companion_quote_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("proxy", logic["route_surface"])
        self.assertIn("price_basis == 'public_companion_underlying_spot_quote'", logic["entry_expression"])
        self.assertEqual("companion_return_strength_bps", logic["edge_expression"])
        self.assertEqual("abs(return_5m_bps)", logic["calculated_features"]["companion_return_strength_bps"])
        requirements = json.loads(row["data_requirements_json"])
        self.assertIn("source_contract_url", requirements["required_fields"])
        self.assertFalse(json.loads(row["risk_gates_json"])["require_route_feasible"])

    def test_anp_quality_verified_companion_quotes_create_canonical_program(self) -> None:
        anp_state = state(
            "quality_verified",
            venue="ANP_BRAZIL_OPC",
            inst_id="ANP:OPC:NEW_EXPLORATORY_BLOCKS:2026-04-14",
            market_surface="anp_oferta_permanente_de_concessao",
            session_status="unknown",
            details={
                "adapter_id": "anp_oferta_permanente_de_concessao",
                "quality_status": "verified_proxy",
                "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
                "route_status": "unknown",
            },
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [anp_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_anp_opc_program", result["actions"][0]["action"])
        self.assertEqual("anp_opc_brazil_upstream_proxy_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("proxy", logic["route_surface"])
        self.assertEqual("available_exploratory_blocks / 25", logic["calculated_features"]["opc_catalogue_depth_signal"])
        self.assertIn("price_basis == 'public_companion_petrobras_adr_quote'", logic["entry_expression"])
        self.assertEqual("opc_reference_intensity + 10 * opc_offshore_bias_pct", logic["edge_expression"])
        requirements = json.loads(row["data_requirements_json"])
        self.assertEqual("anp_oferta_permanente_de_concessao", requirements["adapter_id"])
        self.assertIn("source_programme_url", requirements["required_fields"])
        self.assertIn("offshore_new_blocks", requirements["supported_snapshot_features"])
        risk_gates = json.loads(row["risk_gates_json"])
        self.assertFalse(risk_gates["require_route_feasible"])
        self.assertTrue(risk_gates["synthetic_research_only"])

    def test_icdx_priceable_price_card_creates_canonical_synthetic_research_program(self) -> None:
        icdx_state = state(
            "priceable",
            venue="ICDX",
            inst_id="ICDX:CPOTR:AUG26:YDSP",
            market_surface="icdx_cpotr",
            session_status="previous_settlement_reference",
            blocker_code="quality_unverified",
            details={
                "adapter_id": "indonesia_commodity_derivatives_exchange_icdx",
                "quality_status": "official_price_card",
                "route_status": "unknown",
            },
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [icdx_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_icdx_cpotr_program", result["actions"][0]["action"])
        self.assertEqual("icdx_cpotr_price_card_reference_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("proxy", logic["route_surface"])
        self.assertEqual("abs(cpotr_opening_gap_bps)", logic["calculated_features"]["cpotr_opening_gap_abs_bps"])
        self.assertIn("price_type == 'previous_settlement'", logic["entry_expression"])
        requirements = json.loads(row["data_requirements_json"])
        self.assertEqual("indonesia_commodity_derivatives_exchange_icdx", requirements["adapter_id"])
        self.assertIn("cpotr_opening_gap_bps", requirements["required_fields"])
        self.assertTrue(json.loads(row["risk_gates_json"])["synthetic_research_only"])

    def test_icdx_quality_verified_milestones_create_canonical_companion_program(self) -> None:
        milestone_state = state(
            "quality_verified",
            venue="ICDX",
            inst_id="ICDX:MARKET_MILESTONES",
            market_surface="icdx_exchange_milestones",
            session_status="previous_settlement_reference",
            details={
                "adapter_id": "indonesia_commodity_derivatives_exchange_icdx",
                "quality_status": "verified_proxy",
                "candidate_reject_reason": "public_companion_price_requires_strategy_logic",
                "route_status": "unknown",
            },
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [milestone_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_icdx_milestones_program", result["actions"][0]["action"])
        self.assertEqual("icdx_exchange_milestones_companion_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("proxy", logic["route_surface"])
        self.assertIn("cpotr_opening_gap_bps > 0", logic["long_expression"])
        self.assertIn(
            "years_since_cpotr_launch + years_since_gofx_launch",
            logic["calculated_features"]["milestone_reference_depth_years"],
        )
        requirements = json.loads(row["data_requirements_json"])
        self.assertEqual("indonesia_commodity_derivatives_exchange_icdx", requirements["adapter_id"])
        self.assertIn("source_timeline_url", requirements["required_fields"])
        self.assertIn("years_since_gofx_launch", requirements["required_fields"])
        self.assertTrue(json.loads(row["risk_gates_json"])["synthetic_research_only"])

    def test_carb_closed_priceable_allowance_results_create_canonical_auction_reference_program(self) -> None:
        carb_state = state(
            "priceable",
            venue="CARB_CA_QC",
            inst_id="CARB:CA_QC_AUCTION:48:CURRENT:2026-08-19",
            market_surface="california_quebec_cap_and_invest_joint_allowance_auctions",
            session_status="closed",
            blocker_code="market_closed",
            details={
                "adapter_id": "california_air_resources_board_cap_and_invest",
                "quality_status": "official_auction_result",
                "candidate_reject_reason": "official_allowance_auction_reference_not_order_routable",
                "route_status": "unknown",
            },
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [carb_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json "
            "from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_carb_allowance_program", result["actions"][0]["action"])
        self.assertEqual("carb_joint_allowance_discount_tightness_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("auction_reference", logic["route_surface"])
        self.assertIn("allowance_category == 'current'", logic["entry_expression"])
        self.assertIn("term_discount_bps <= 35", logic["entry_expression"])
        self.assertEqual("max(0,-term_discount_zscore)", logic["calculated_features"]["discount_quality_signal"])
        requirements = json.loads(row["data_requirements_json"])
        self.assertEqual("california_air_resources_board_cap_and_invest", requirements["adapter_id"])
        self.assertIn("term_discount_zscore", requirements["supported_snapshot_features"])
        risk_gates = json.loads(row["risk_gates_json"])
        self.assertFalse(risk_gates["require_route_feasible"])
        self.assertTrue(risk_gates["synthetic_research_only"])
        self.assertEqual(0, self.conn.execute("select count(*) from market_hunter_directives").fetchone()[0])

    def test_aofm_priceable_tender_results_create_canonical_auction_reference_program(self) -> None:
        aofm_state = state(
            "priceable",
            venue="AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT",
            inst_id="AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT:TBOND:RESULT:AU000XCLWAM8:2026-08-05:2",
            market_surface="australian_treasury_bond_tenders_and_results",
            session_status="results_published",
            blocker_code="quality_unverified",
            details={
                "adapter_id": "australian_office_of_financial_management_aofm",
                "quality_status": "official_auction_result",
                "candidate_reject_reason": "official_auction_result_not_executable_quote",
                "route_status": "unknown",
            },
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [aofm_state]}
        )
        row = self.conn.execute(
            "select strategy_lab_id, strategy_logic_json, data_requirements_json, risk_gates_json "
            "from strategy_lab_experiments"
        ).fetchone()

        self.assertEqual(1, result["summary"]["actions_created"])
        self.assertEqual("strategy_lab_aofm_tender_program", result["actions"][0]["action"])
        self.assertEqual("aofm_treasury_bond_tender_strength_v1", row["strategy_lab_id"])
        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("observation_program", logic["type"])
        self.assertEqual("auction_reference", logic["route_surface"])
        self.assertIn("auction_coverage_ratio >= 2", logic["entry_expression"])
        self.assertIn("aofm_demand_pressure", logic["edge_expression"])
        requirements = json.loads(row["data_requirements_json"])
        self.assertEqual("australian_office_of_financial_management_aofm", requirements["adapter_id"])
        self.assertIn("isin", requirements["required_fields"])
        self.assertIn("auction_average_yield_pct", requirements["supported_snapshot_features"])
        risk_gates = json.loads(row["risk_gates_json"])
        self.assertFalse(risk_gates["require_route_feasible"])
        self.assertTrue(risk_gates["synthetic_research_only"])
        self.assertEqual(0, self.conn.execute("select count(*) from market_hunter_directives").fetchone()[0])

    def test_spot_borrow_user_constraint_suppresses_route_task(self) -> None:
        item = state(
            "strategy_candidate",
            strategy_lineage="frontier_crypto|short_frontier_spot",
            blocker_code="route_spot_borrow",
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [item]}
        )
        self.assertEqual(1, result["summary"]["user_constraints_suppressed"])
        self.assertEqual(0, self.conn.execute("select count(*) from route_probe_tasks").fetchone()[0])

    def test_closed_market_creates_no_action(self) -> None:
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn,
            self.settings,
            {"states": [state("quality_verified", session_status="closed", blocker_code="market_closed")]},
        )
        self.assertEqual([], result["actions"])

    def test_advancing_stage_resolves_prior_enrichment_topic(self) -> None:
        first_state = state("priceable", blocker_code="quality_unverified")
        market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [first_state]}
        )
        advanced = state("quality_verified", admission_key=first_state["admission_key"], blocker_code=None)
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [advanced]}
        )
        self.assertEqual(1, result["summary"]["prior_stage_topics_resolved"])
        status = self.conn.execute("select status from market_hunter_directives").fetchone()[0]
        self.assertEqual("resolved_market_admission_advanced", status)


if __name__ == "__main__":
    unittest.main()
