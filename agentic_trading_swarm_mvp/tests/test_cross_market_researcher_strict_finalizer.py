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
    finalize_recommendation_response,
)


def _payload(**overrides):
    payload = {
        "action": "propose_diagnostic_hypothesis",
        "priority": 82,
        "title": "Compare paper-market outcomes",
        "rationale": "The observed contexts have different paper-only outcomes.",
        "market_key": "paper_cross_market",
        "evidence": {"sample_count": 12},
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
        estimated_cost_usd=0.0,
        api="test",
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

        for response in (top_level_array, missing_field, invalid_action, invalid_priority):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    finalize_cross_market_researcher_response(json.dumps(response))


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


if __name__ == "__main__":
    unittest.main()
