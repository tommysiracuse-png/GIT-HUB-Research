import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_swarm_runner
from recommendation_schema import (
    finalize_cross_market_researcher_response,
    finalize_red_team_response,
    finalize_recommendation_response,
)


def _payload(**overrides):
    payload = {
        "action": "propose_diagnostic_hypothesis",
        "priority": 82,
        "title": "Compare paper-market outcomes",
        "rationale": "The observed contexts have different paper-only outcomes.",
        "market_key": "paper_cross_market",
        "evidence": {
            "sample_count": 12,
            "market_count": 3,
            "supporting_markets": "okx_spot, bybit_spot, binance_spot",
            "support_summary": "Matched paper-only contexts aligned across three sampled markets.",
        },
        "proposed_change": {"analysis": "Compare the reliable labels."},
    }
    payload.update(overrides)
    return payload


def _model_result(text):
    return SimpleNamespace(
        text=text,
        model_name="test-model",
        model_tier="fast",
        status="model_call:test",
        stop_reason="completed",
        estimated_cost_usd=0.0,
        api="test",
        prompt_tokens=120,
        completion_tokens=30,
        max_output_tokens=400,
        reasoning_effort="low",
        reasoning_mode="test",
        verbosity="low",
        structured_json=True,
    )


class RecommendationFinalizerTests(unittest.TestCase):
    def test_accepts_one_complete_object(self):
        payload = _payload()

        self.assertEqual(finalize_recommendation_response(json.dumps(payload)), payload)

    def test_rejects_wrappers_partial_json_arrays_and_missing_fields(self):
        valid = json.dumps(_payload())
        invalid_responses = (
            f"```json\n{valid}\n```",
            f"Recommendation: {valid}",
            valid + " trailing commentary",
            valid + valid,
            valid[:-1],
            f"[{valid}]",
            json.dumps({"action": "propose_diagnostic_hypothesis"}),
        )

        for response in invalid_responses:
            with self.subTest(response=response[:40]):
                with self.assertRaises(ValueError):
                    finalize_recommendation_response(response)

    def test_rejects_non_standard_constants_and_duplicate_keys(self):
        payload = json.dumps(_payload())
        duplicate_title = payload.replace(
            '"title": "Compare paper-market outcomes",',
            '"title": "Compare paper-market outcomes", "title": "Conflicting title",',
        )
        non_standard_number = payload.replace('"priority": 82', '"priority": NaN')

        for response in (duplicate_title, non_standard_number):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    finalize_recommendation_response(response)

    def test_cross_market_schema_rejects_contract_violations(self):
        top_level_array = [_payload()]
        missing_field = _payload()
        missing_field.pop("evidence")
        invalid_action = _payload(action="propose_code_change")
        invalid_priority = _payload(priority="high")
        extra_field = _payload(extra="not allowed")
        empty_evidence = _payload(evidence={})
        string_proposed_change = _payload(proposed_change="partial")
        non_paper_market_key = _payload(market_key="global_cross_market")
        blank_evidence = _payload(evidence={"sample_count": ""})
        blank_proposed_change = _payload(proposed_change={"analysis": "   "})
        unsupported_evidence_shape = _payload(evidence={"note": "No support facts or diagnostic markers"})
        insufficient_structured_evidence = _payload(evidence={"sample_count": 12})

        for response in (
            top_level_array,
            missing_field,
            invalid_action,
            invalid_priority,
            extra_field,
            empty_evidence,
            string_proposed_change,
            non_paper_market_key,
            blank_evidence,
            blank_proposed_change,
            unsupported_evidence_shape,
            insufficient_structured_evidence,
        ):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    finalize_cross_market_researcher_response(json.dumps(response))

    def test_red_team_schema_rejects_wrappers_and_extra_fields(self):
        wrapped = f"Recommendation: {json.dumps(_payload())}"
        extra_field = _payload(extra="not allowed")

        for response in (wrapped, json.dumps(extra_field)):
            with self.subTest(response=response[:60]):
                with self.assertRaises(ValueError):
                    finalize_red_team_response(response)

    def test_red_team_schema_rejects_out_of_range_priority(self):
        invalid_priority = _payload(priority=0)

        with self.assertRaisesRegex(
            ValueError,
            "red-team recommendation priority must be an integer between 1 and 100",
        ):
            finalize_red_team_response(json.dumps(invalid_priority))


class CrossMarketResearcherRetryTests(unittest.TestCase):
    def setUp(self):
        self.agent = next(
            agent
            for agent in llm_swarm_runner.AGENTS
            if agent["name"] == "cross_market_researcher"
        )
        self.packet = {
            "allowed_recommendation_actions": ["propose_diagnostic_hypothesis"],
            "growth_experiments": [],
        }

    def test_wrapped_object_is_rejected_instead_of_recovered(self):
        response = f"Here is the result: {json.dumps(_payload())}"

        rec = llm_swarm_runner.parse_recommendation(response, self.agent, self.packet)

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["parse_status"], "invalid_json")
        self.assertEqual(rec["terminal_failure_reason"], "extra_text_or_invalid_json")

    def test_missing_required_field_retries_once_and_accepts_valid_retry(self):
        incomplete = _payload()
        incomplete.pop("proposed_change")
        results = [
            _model_result(json.dumps(incomplete)),
            _model_result(json.dumps(_payload(title="Retry produced a complete object"))),
        ]

        with mock.patch.object(llm_swarm_runner, "complete", side_effect=results) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(complete.call_args.kwargs["operation"], "llm_swarm_schema_retry")
        self.assertEqual(rec["title"], "Retry produced a complete object")
        self.assertEqual(rec["retry_count"], 1)
        self.assertEqual(rec["initial_parse_status"], "invalid_schema")

    def test_invalid_retry_becomes_paper_only_schema_diagnostic(self):
        wrapped = f"```json\n{json.dumps(_payload())}\n```"

        with mock.patch.object(
            llm_swarm_runner,
            "complete",
            side_effect=[_model_result(wrapped), _model_result(wrapped)],
        ) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(rec["action"], "propose_diagnostic_hypothesis")
        self.assertEqual(rec["parse_status"], "schema_fallback")
        self.assertTrue(rec["evidence"]["paper_only"])
        self.assertIn("exactly one complete JSON object", rec["evidence"]["schema_violation"])
        self.assertEqual(rec["evidence"]["raw_generation_metadata"]["retry"]["response_text"], wrapped)
        self.assertEqual(rec["retry_count"], 1)
        self.assertTrue(rec["evidence"]["insufficient_market_evidence_defaults_to_diagnostic"])
        self.assertTrue(rec["evidence"]["insufficient_structured_evidence"])
        self.assertTrue(rec["evidence"]["market_recommendation_blocked"])
        self.assertEqual(
            rec["proposed_change"]["fallback_mode"],
            "paper_only_diagnostic_hypothesis",
        )
        self.assertIn("sufficient cross-market evidence", rec["proposed_change"]["next_step"])

    def test_invalid_action_becomes_schema_diagnostic_after_retry(self):
        invalid_action = json.dumps(_payload(action="propose_code_change"))

        with mock.patch.object(
            llm_swarm_runner,
            "complete",
            side_effect=[_model_result(invalid_action), _model_result(invalid_action)],
        ) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(rec["action"], "propose_diagnostic_hypothesis")
        self.assertEqual(rec["evidence"]["raw_generation_metadata"]["initial"]["response_text"], invalid_action)
        self.assertIn("action is not allowed", rec["evidence"]["schema_violation"])

    def test_final_guard_replaces_invalid_post_processor_output_with_no_action(self):
        agent = next(
            item for item in llm_swarm_runner.AGENTS if item["name"] == "market_scout"
        )
        raw = json.dumps(
            {
                "action": "propose_hunter_directive",
                "priority": 60,
                "title": "Raw response is complete",
                "rationale": "Use this only to verify audit separation.",
                "market_key": "paper.okx",
                "evidence": {},
                "proposed_change": "Record the parser result.",
            }
        )
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"]}

        with mock.patch.object(llm_swarm_runner, "complete", return_value=_model_result(raw)), mock.patch.object(
            llm_swarm_runner, "parse_recommendation", return_value={}
        ):
            rec = llm_swarm_runner.run_agent(agent, packet, [])

        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["action"], "no_action")
        self.assertEqual(rec["parse_status"], "schema_fallback")
        audit = rec["model_output_audit"]
        self.assertEqual(audit["initial"]["raw_model_output"], raw)
        self.assertEqual(audit["initial"]["post_processor_output"], "{}")
        self.assertTrue(audit["initial"]["transport_integrity"]["raw_schema_valid"])
        self.assertEqual(
            audit["initial"]["transport_integrity"]["cutoff_assessment"],
            "not_detected_complete_schema_object_below_token_limit",
        )

    def test_transport_audit_flags_truncated_output_at_configured_token_limit(self):
        result = _model_result('{"action":"propose_hunter_directive"')
        result.completion_tokens = 800
        result.max_output_tokens = 800

        integrity = llm_swarm_runner._transport_integrity(result.text, result)

        self.assertTrue(integrity["truncation_suspected"])
        self.assertTrue(integrity["token_limit_reached"])
        self.assertEqual(integrity["cutoff_assessment"], "token_limit_cutoff_suspected")

    def test_generation_metadata_records_stop_reason_and_payload_size(self):
        raw = json.dumps(_payload(title="utf8 title cafe"))
        result = _model_result(raw)

        metadata = llm_swarm_runner._generation_metadata(result)

        self.assertEqual(metadata["prompt_tokens"], 120)
        self.assertEqual(metadata["completion_tokens"], 30)
        self.assertEqual(metadata["total_tokens"], 150)
        self.assertEqual(metadata["token_count"], 150)
        self.assertEqual(metadata["stop_reason"], "completed")
        self.assertEqual(metadata["finish_reason"], "completed")
        self.assertEqual(
            metadata["transport_integrity"]["raw_payload_size_bytes"],
            len(raw.encode("utf-8")),
        )
        self.assertEqual(metadata["transport_integrity"]["prompt_tokens"], 120)
        self.assertEqual(metadata["transport_integrity"]["total_tokens"], 150)
        self.assertEqual(metadata["transport_integrity"]["token_count"], 150)
        self.assertEqual(metadata["transport_integrity"]["stop_reason"], "completed")
        self.assertEqual(metadata["transport_integrity"]["finish_reason"], "completed")

    def test_post_processor_audit_records_serialization_exception(self):
        attempt = {}

        llm_swarm_runner._record_post_processor_output(
            attempt,
            {
                "action": "no_action",
                "priority": 1,
                "title": "Schema guard",
                "rationale": "Record the serialization failure.",
                "market_key": "paper.market_scout.schema_guard",
                "evidence": {"issue": "serializer"},
                "proposed_change": {"summary": float("nan")},
            },
        )

        self.assertIsNone(attempt["post_processor_output"])
        self.assertFalse(attempt["post_processor_schema_valid"])
        self.assertIn("ValueError", attempt["post_processor_serialization_error"])


class RedTeamStrictRetryTests(unittest.TestCase):
    def setUp(self):
        self.agent = next(
            agent
            for agent in llm_swarm_runner.AGENTS
            if agent["name"] == "red_team"
        )
        self.packet = {
            "allowed_recommendation_actions": [
                "propose_diagnostic_hypothesis",
                "propose_code_change",
            ],
            "growth_experiments": [],
        }

    def test_invalid_action_retries_once_and_accepts_valid_retry(self):
        invalid_action = json.dumps(
            {
                "action": "propose_code_change",
                "priority": 82,
                "title": "Repair the failure family",
                "rationale": "The route should be fixed in code.",
                "market_key": "paper.red_team",
                "evidence": {"issue": "schema"},
                "proposed_change": {"summary": "Open a build task."},
            }
        )
        valid_retry = json.dumps(
            {
                "action": "propose_diagnostic_hypothesis",
                "priority": 79,
                "title": "Retry produced a diagnostic hypothesis",
                "rationale": "The loss pattern still needs paper-only diagnosis.",
                "market_key": "paper.red_team.okx",
                "evidence": {"issue": "basis decay"},
                "proposed_change": {"summary": "Measure decay versus route quality."},
            }
        )

        with mock.patch.object(
            llm_swarm_runner,
            "complete",
            side_effect=[_model_result(invalid_action), _model_result(valid_retry)],
        ) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(complete.call_args.kwargs["operation"], "llm_swarm_schema_retry")
        self.assertEqual(rec["title"], "Retry produced a diagnostic hypothesis")
        self.assertEqual(rec["retry_count"], 1)
        self.assertEqual(rec["initial_parse_status"], "invalid_action")

    def test_invalid_retry_becomes_red_team_schema_diagnostic(self):
        wrapped = f"```json\n{json.dumps(_payload())}\n```"

        with mock.patch.object(
            llm_swarm_runner,
            "complete",
            side_effect=[_model_result(wrapped), _model_result(wrapped)],
        ) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(rec["action"], "propose_diagnostic_hypothesis")
        self.assertEqual(rec["parse_status"], "schema_fallback")
        self.assertTrue(rec["evidence"]["paper_only"])
        self.assertIn("exactly one complete JSON object", rec["evidence"]["schema_violation"])
        self.assertEqual(rec["evidence"]["raw_generation_metadata"]["retry"]["response_text"], wrapped)
        self.assertEqual(rec["retry_count"], 1)

    def test_out_of_range_priority_retries_once_and_accepts_valid_retry(self):
        invalid_priority = json.dumps(
            {
                "action": "propose_diagnostic_hypothesis",
                "priority": 0,
                "title": "Priority is invalid",
                "rationale": "This should be retried instead of being accepted.",
                "market_key": "paper.red_team",
                "evidence": {"issue": "schema"},
                "proposed_change": {"summary": "Repair the response."},
            }
        )
        valid_retry = json.dumps(
            {
                "action": "propose_diagnostic_hypothesis",
                "priority": 50,
                "title": "Retry repaired the priority",
                "rationale": "The follow-up response satisfied the strict contract.",
                "market_key": "paper.red_team.okx",
                "evidence": {"issue": "priority range"},
                "proposed_change": {"summary": "Measure the failure before changing code."},
            }
        )

        with mock.patch.object(
            llm_swarm_runner,
            "complete",
            side_effect=[_model_result(invalid_priority), _model_result(valid_retry)],
        ) as complete:
            rec = llm_swarm_runner.run_agent(self.agent, self.packet, [])

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(complete.call_args.kwargs["operation"], "llm_swarm_schema_retry")
        self.assertEqual(rec["title"], "Retry repaired the priority")
        self.assertEqual(rec["retry_count"], 1)
        self.assertEqual(rec["initial_parse_status"], "invalid_schema")


class CrossMarketSchemaRetryPromptTests(unittest.TestCase):
    def test_cross_market_initial_prompt_requires_structured_support_or_blocked_diagnostic(self):
        agent = next(
            agent
            for agent in llm_swarm_runner.AGENTS
            if agent["name"] == "cross_market_researcher"
        )

        prompt = llm_swarm_runner.agent_prompt(
            agent,
            {"allowed_recommendation_actions": ["propose_diagnostic_hypothesis", "no_action"]},
            [],
        )

        self.assertIn("explicit cross-market support facts in-schema", prompt)
        self.assertIn("sample_count", prompt)
        self.assertIn("supporting_markets", prompt)
        self.assertIn("insufficient_structured_evidence=true", prompt)

    def test_cross_market_retry_prompt_requires_diagnostic_fallback_on_insufficient_evidence(self):
        agent = next(
            agent
            for agent in llm_swarm_runner.AGENTS
            if agent["name"] == "cross_market_researcher"
        )

        prompt = llm_swarm_runner._schema_retry_prompt(agent, '{"action":"partial"}')

        self.assertIn("Use exactly these top-level keys", prompt)
        self.assertIn("paper-only diagnostic hypothesis", prompt)
        self.assertIn("no extra keys", prompt)
        self.assertIn("evidence and proposed_change must be non-empty JSON objects", prompt)
        self.assertIn("Every required key must be present and non-empty", prompt)
        self.assertIn("blocked until sufficient cross-market evidence is supplied in-schema", prompt)
        self.assertIn("insufficient_structured_evidence", prompt)


if __name__ == "__main__":
    unittest.main()
