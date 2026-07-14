from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import code_evolution
import llm_bridge
import storage


def proposal(diff: str, **overrides: object) -> dict:
    payload = {
        "action": "propose_code_change",
        "priority": 90,
        "title": "Improve report wording",
        "rationale": "Report clarity helps monitor the paper system.",
        "change_category": "report_dashboard",
        "expected_files": ["README.md"],
        "tests_to_run": [],
        "rollback_criteria": "Revert if tests fail or reports stop refreshing.",
        "frontier_escalation_reason": "Build planning requires frontier reasoning.",
        "model": {"name": "openai/gpt-5.6-sol", "tier": "frontier"},
        "evidence": {"report": "runs/self_improvement_report.md"},
        "unified_diff": diff,
        "proposed_change": "Add a paper-only report note.",
    }
    payload.update(overrides)
    return payload


def settings(**overrides: object) -> dict:
    cfg = {
        "allow_live_trading": False,
        "code_evolution": {
            "enabled": True,
            "auto_merge_paper_only": True,
            "max_auto_merges_per_loop": 1,
            "require_frontier_model": True,
            "required_model": "openai/gpt-5.6-sol",
            "min_priority": 80,
            "run_full_regression": False,
            "probation_loops": 2,
            "rollback_on_health_failure": True,
            "generate_patch_when_missing": False,
        },
    }
    cfg["code_evolution"].update(overrides)
    return cfg


class CodeEvolutionGovernorTests(unittest.TestCase):
    def test_scan_allows_safe_report_patch(self) -> None:
        diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 hello
+paper-only note
"""
        safety = code_evolution.validate_and_scan(proposal(diff), diff, settings())
        self.assertTrue(safety["allowed"], safety)
        self.assertEqual(safety["changed_files"], ["README.md"])

    def test_scan_blocks_live_trading_and_credentials(self) -> None:
        diff = """diff --git a/config/settings.example.json b/config/settings.example.json
--- a/config/settings.example.json
+++ b/config/settings.example.json
@@ -1 +1,4 @@
 {}
+{"allow_live_trading": true}
+OPENAI_API_KEY = "secret"
"""
        safety = code_evolution.validate_and_scan(
            proposal(diff, expected_files=["config/settings.example.json"]),
            diff,
            settings(),
        )
        self.assertFalse(safety["allowed"])
        self.assertIn("enables_live_trading", safety["reasons"])
        self.assertIn("touches_credentials", safety["reasons"])

    def test_scan_blocks_startup_path_and_raw_installer_command(self) -> None:
        diff = """diff --git a/scripts/start.ps1 b/scripts/start.ps1
--- a/scripts/start.ps1
+++ b/scripts/start.ps1
@@ -0,0 +1,2 @@
+pip install unknown-package
+Register-ScheduledTask -TaskName demo
"""
        safety = code_evolution.validate_and_scan(
            proposal(diff, expected_files=["scripts/start.ps1"], change_category="tests_fixtures"),
            diff,
            settings(),
        )
        self.assertFalse(safety["allowed"])
        self.assertIn("path_not_allowed:scripts/start.ps1", safety["reasons"])
        self.assertIn("installer_command_in_code", safety["reasons"])
        self.assertIn("startup_or_system_task", safety["reasons"])

    def test_scan_allows_repo_dependency_manifest_change(self) -> None:
        diff = """diff --git a/requirements-autonomous.txt b/requirements-autonomous.txt
--- a/requirements-autonomous.txt
+++ b/requirements-autonomous.txt
@@ -1 +1,2 @@
 # packages
+orjson==3.10.18
"""
        safety = code_evolution.validate_and_scan(
            proposal(
                diff,
                expected_files=["requirements-autonomous.txt"],
                change_category="dependency_management",
                evidence={"reason": "Autonomous builder needs a tested PyPI package for parser performance."},
            ),
            diff,
            settings(run_full_regression=False),
        )
        self.assertTrue(safety["allowed"], safety["reasons"])

    def test_scan_allows_runtime_pipeline_integration(self) -> None:
        diff = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+FRONTIER_RUNTIME_INTEGRATION_ENABLED = True
"""
        safety = code_evolution.validate_and_scan(
            proposal(
                diff,
                change_category="runtime_pipeline_integration",
                expected_files=["src/llm_bridge.py"],
                evidence={"reason": "Wire generated dashboard output into the state packet."},
            ),
            diff,
            settings(),
        )
        self.assertTrue(safety["allowed"], safety)
        self.assertEqual(safety["category"], "runtime_pipeline_integration")
        self.assertEqual(safety["implementation_mode"], "runtime_active")

    def test_scan_accepts_string_evidence_as_structured_evidence(self) -> None:
        diff = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+STRING_EVIDENCE_ACCEPTED = True
"""
        payload = proposal(
            diff,
            change_category="runtime_pipeline_integration",
            expected_files=["src/llm_bridge.py"],
            evidence="Previous recommendation response was malformed and blocked parser automation.",
        )

        safety = code_evolution.validate_and_scan(payload, diff, settings())

        self.assertTrue(safety["allowed"], safety)
        self.assertNotIn("missing_evidence", safety["reasons"])

    def test_market_expansion_defaults_to_runtime_active_and_can_use_standard_model(self) -> None:
        diff = """diff --git a/src/frontier_data_quality.py b/src/frontier_data_quality.py
--- a/src/frontier_data_quality.py
+++ b/src/frontier_data_quality.py
@@ -1 +1,2 @@
 # quality
+MARKET_EXPANSION_ACTIVE = True
"""
        payload = proposal(
            diff,
            title="Expand starved venue quality coverage",
            change_category="scanner_expansion",
            expected_files=["src/frontier_data_quality.py"],
            tests_to_run=[],
            model={"name": "openai/gpt-5.4", "tier": "standard"},
            frontier_escalation_reason=None,
            evidence={"known_quality_rate": 0.12, "target": 0.25},
            proposed_change="Increase market coverage through runtime depth-selection logic.",
        )

        preflight = code_evolution.preflight_proposal(payload, settings())
        safety = code_evolution.validate_and_scan(payload, diff, settings(), preflight=preflight)

        self.assertEqual(preflight["implementation_mode"], "runtime_active")
        self.assertTrue(safety["allowed"], safety)
        self.assertFalse(safety["frontier_required"])

    def test_frontier_is_still_required_for_risky_paper_policy(self) -> None:
        diff = """diff --git a/src/strategy_reliability.py b/src/strategy_reliability.py
--- a/src/strategy_reliability.py
+++ b/src/strategy_reliability.py
@@ -1 +1,2 @@
 # strategy
+PAPER_POLICY_GATE = True
"""
        payload = proposal(
            diff,
            change_category="paper_scoring_logic",
            expected_files=["src/strategy_reliability.py"],
            model={"name": "openai/gpt-5.4", "tier": "standard"},
            frontier_escalation_reason=None,
        )

        safety = code_evolution.validate_and_scan(
            payload,
            diff,
            settings(frontier_required_categories=["paper_scoring_logic"], frontier_required_priority=80),
            preflight=code_evolution.preflight_proposal(
                payload,
                settings(frontier_required_categories=["paper_scoring_logic"], frontier_required_priority=80),
            ),
        )

        self.assertFalse(safety["allowed"])
        self.assertIn("required_frontier_model_missing", safety["reasons"])
        self.assertEqual(safety["implementation_mode"], "paper_policy")

    def test_default_patch_generation_tier_is_fast_with_standard_for_high_quality(self) -> None:
        payload = proposal(
            "",
            title="Expand starved venue quality coverage",
            change_category="scanner_expansion",
            expected_files=["src/frontier_data_quality.py"],
            evidence={"known_quality_rate": 0.12, "target": 0.25},
            proposed_change="Increase runtime market coverage through depth-selection logic.",
        )
        cfg = {
            "allow_live_trading": False,
            "code_evolution": {
                "enabled": True,
                "require_frontier_model": False,
            },
        }
        preflight = code_evolution.preflight_proposal(payload, cfg)

        self.assertEqual(code_evolution._patch_generation_tier(payload, cfg, preflight=preflight), "standard")

        weak = proposal(
            "",
            change_category="report_dashboard",
            expected_files=["README.md"],
            proposed_change="Add a standalone note.",
        )
        weak_preflight = code_evolution.preflight_proposal(weak, cfg)

        self.assertEqual(code_evolution._patch_generation_tier(weak, cfg, preflight=weak_preflight), "fast")

    def test_public_adapter_can_merge_with_standard_patch_generation(self) -> None:
        diff = """diff --git a/src/frontier_crypto_adapter.py b/src/frontier_crypto_adapter.py
--- a/src/frontier_crypto_adapter.py
+++ b/src/frontier_crypto_adapter.py
@@ -1 +1,2 @@
 # adapter
+PUBLIC_ADAPTER_PATCH = True
"""
        payload = proposal(
            diff,
            change_category="public_data_adapter",
            expected_files=["src/frontier_crypto_adapter.py"],
            model={"name": "openai/gpt-5.4", "tier": "standard"},
            frontier_escalation_reason=None,
        )
        patch_generation = {
            "status": "model_call:responses",
            "model_name": "openai/gpt-5.4",
            "model_tier": "standard",
            "requested_model_tier": "standard",
        }

        safety = code_evolution.validate_and_scan(
            payload,
            diff,
            settings(),
            patch_generation=patch_generation,
            preflight=code_evolution.preflight_proposal(payload, settings()),
        )

        self.assertTrue(safety["allowed"], safety)
        self.assertFalse(safety["frontier_required"])

    def test_invalid_implementation_mode_blocks_before_model_spend(self) -> None:
        payload = proposal(
            "",
            change_category="scanner_expansion",
            implementation_mode="live_runtime",
            expected_files=["src/frontier_data_quality.py"],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())
        safety = code_evolution.validate_and_scan(payload, "", settings(), preflight=preflight)

        self.assertEqual(preflight["quality_scorecard"]["preflight_reject_status"], "rejected_preflight_invalid_implementation_mode")
        self.assertEqual(safety["decision"], "rejected_preflight_invalid_implementation_mode")

    def test_missing_or_unknown_category_blocks_before_model_spend(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 50,
            "title": "build_planner unstructured recommendation",
            "rationale": "",
            "evidence": {"parser": "fallback"},
            "proposed_change": "",
            "model": {"name": "openai/gpt-5.4", "tier": "standard"},
        }

        preflight = code_evolution.preflight_proposal(payload, settings())
        safety = code_evolution.validate_and_scan(payload, "", settings(), preflight=preflight)

        self.assertEqual(preflight["quality_scorecard"]["preflight_reject_status"], "rejected_preflight_invalid_category")
        self.assertEqual(safety["decision"], "rejected_preflight_invalid_category")
        self.assertIn("category_not_allowed:missing", safety["reasons"])

    def test_scan_allows_paper_only_policy_and_evolution_repairs(self) -> None:
        for category, path in [
            ("self_improvement_policy", "src/self_improvement.py"),
            ("evolution_loop_improvement", "src/code_evolution.py"),
            ("paper_scoring_logic", "src/frontier_crypto_adapter.py"),
        ]:
            with self.subTest(category=category):
                diff = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 # module
+PAPER_ONLY_AUTONOMY = True
"""
                safety = code_evolution.validate_and_scan(
                    proposal(
                        diff,
                        change_category=category,
                        expected_files=[path],
                        evidence={"reason": f"{category} is paper-only and reversible."},
                    ),
                    diff,
                    settings(),
                )
                self.assertTrue(safety["allowed"], safety)
                self.assertEqual(safety["category"], category)

    def test_nested_code_change_shape_is_accepted(self) -> None:
        diff = """diff --git a/src/route_intelligence.py b/src/route_intelligence.py
--- a/src/route_intelligence.py
+++ b/src/route_intelligence.py
@@ -0,0 +1,2 @@
+def build_read_only_matrix():
+    return {}
"""
        payload = {
            "action": "propose_code_change",
            "priority": 92,
            "title": "Read-only route matrix",
            "model": {
                "name": "openai/gpt-5.6-sol",
                "tier": "frontier",
                "frontier_escalation_reason": "Route blockers affect many paper opportunities.",
            },
            "code_change": {
                "change_category": "read_only_route_intelligence",
                "expected_files": ["src/route_intelligence.py"],
                "tests_to_run": [],
                "rollback_criteria": "Revert if tests fail.",
                "evidence": {"route_blockers": {"spot_borrow": 30}},
                "unified_diff": diff,
            },
            "proposed_change": "Create read-only route matrix.",
        }
        safety = code_evolution.validate_and_scan(payload, diff, settings())
        self.assertTrue(safety["allowed"], safety)
        self.assertEqual(safety["category"], "read_only_route_intelligence")
        self.assertEqual(safety["changed_files"], ["src/route_intelligence.py"])

    def test_patch_generation_uses_category_default_files_for_descriptive_paths(self) -> None:
        payload = proposal(
            "",
            change_category=None,
            expected_files=[],
            code_change={
                "change_category": "quality_scoring",
                "expected_files": ["existing frontier crypto scanner module"],
                "evidence": {"quality_80_100_avg_pnl_bps": 32.0},
            },
        )
        with mock.patch.object(code_evolution, "complete") as complete:
            complete.return_value = type(
                "Result",
                (),
                {
                    "text": "diff --git a/src/frontier_crypto_adapter.py b/src/frontier_crypto_adapter.py\n",
                    "status": "model_call:responses",
                    "model_name": "openai/gpt-5.6-sol",
                    "model_tier": "frontier",
                    "estimated_cost_usd": 0.01,
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                },
            )()
            diff, meta = code_evolution.generate_patch_with_frontier_model(payload, settings())
        self.assertIn("src/frontier_crypto_adapter.py", complete.call_args.args[1])
        self.assertIn("BUILDER_CONTEXT version=1", complete.call_args.args[1])
        self.assertTrue(diff.startswith("diff --git"))
        self.assertEqual(meta["status"], "model_call:responses")
        self.assertEqual(meta["builder_context"]["version"], 1)

    def test_patch_repair_reloads_current_builder_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text(
                "def current_function():\n"
                "    return 'current file text'\n",
                encoding="utf-8",
            )
            payload = proposal(
                "",
                expected_files=["src/llm_bridge.py"],
                change_category="llm_prompt_state_packet",
                tests_to_run=[],
            )
            diff = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -99 +99,2 @@
 missing stale line
+paper-only note
"""
            with mock.patch.object(code_evolution, "complete") as complete:
                complete.return_value = type(
                    "Result",
                    (),
                    {
                        "text": "diff --git a/src/llm_bridge.py b/src/llm_bridge.py\n",
                        "status": "model_call:responses",
                        "model_name": "openai/gpt-5.4-mini",
                        "model_tier": "fast",
                        "estimated_cost_usd": 0.0,
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                    },
                )()
                _repaired, meta = code_evolution.repair_patch_with_frontier_model(
                    payload,
                    settings(repair_patch_tier="fast"),
                    diff,
                    {"stage": "patch_check", "commands": [{"stderr_tail": "patch failed"}]},
                    root=root,
                )

        prompt = complete.call_args.args[1]
        self.assertIn("BUILDER_CONTEXT version=1", prompt)
        self.assertIn("current_function", prompt)
        self.assertIn("current file text", prompt)
        self.assertEqual(meta["builder_context"]["files"][0]["path"], "src/llm_bridge.py")

    def test_preflight_repairs_known_bad_paths(self) -> None:
        payload = proposal(
            "",
            change_category="public_data_adapter",
            expected_files=["src/radar/frontier_crypto_venues.py", "tests/test_frontier_crypto_venues.py"],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn("src/frontier_crypto_adapter.py", preflight["target_files"])
        self.assertIn("tests/test_frontier_crypto_adapter.py", preflight["target_files"])
        self.assertEqual(len(preflight["path_repairs"]), 2)

    def test_preflight_repairs_rootless_modules_and_new_fixtures(self) -> None:
        payload = proposal(
            "",
            change_category=None,
            expected_files=[],
            code_change={
                "change_category": "evolution_loop_improvement",
                "implementation_mode": "runtime_active",
                "expected_files": [
                    "llm_recommendation_ingestion.py",
                    "self_improvement.py",
                    "tests/test_llm_recommendation_ingestion.py",
                    "tests/fixtures/llm_recommendations/native_valid.json",
                ],
                "tests_to_run": [
                    "python -m unittest tests.test_llm_recommendation_ingestion",
                ],
            },
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn("src/llm_recommendation_ingestion.py", preflight["target_files"])
        self.assertIn("src/self_improvement.py", preflight["target_files"])
        self.assertIn("tests/test_llm_recommendation_ingestion.py", preflight["target_files"])
        self.assertIn("tests/fixtures/llm_recommendations/native_valid.json", preflight["target_files"])
        self.assertFalse(preflight["invalid_targets"])
        self.assertFalse(preflight["test_issues"])
        self.assertEqual(preflight["quality_scorecard"]["preflight_reject_status"], None)

    def test_rewrite_diff_paths_repairs_rootless_generated_diff(self) -> None:
        diff = """diff --git a/llm_recommendation_ingestion.py b/llm_recommendation_ingestion.py
--- /dev/null
+++ b/llm_recommendation_ingestion.py
@@ -0,0 +1,2 @@
+VALUE = 1
+
"""

        rewritten = code_evolution.rewrite_diff_paths(diff)

        self.assertIn("diff --git a/src/llm_recommendation_ingestion.py b/src/llm_recommendation_ingestion.py", rewritten)
        self.assertIn("+++ b/src/llm_recommendation_ingestion.py", rewritten)
        self.assertEqual(code_evolution.changed_files_from_diff(rewritten), ["src/llm_recommendation_ingestion.py"])

    def test_preflight_repairs_recurring_bad_test_commands(self) -> None:
        payload = proposal(
            "",
            change_category="parser_improvement",
            expected_files=["src/prediction_markets_adapter.py", "tests/test_prediction_markets_adapter.py"],
            tests_to_run=["pytest tests/test_prediction_markets_adapter.py -q"],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn("src/prediction_market_scanner.py", preflight["target_files"])
        self.assertIn("tests/test_prediction_market_scanner.py", preflight["target_files"])
        self.assertFalse(preflight["test_issues"])
        self.assertEqual(preflight["parsed_tests"][0][-1], "tests/test_prediction_market_scanner.py")

    def test_preflight_repairs_bitso_depth_targets(self) -> None:
        payload = proposal(
            "",
            title="Wire Bitso public depth into frontier scanner",
            change_category="public_data_adapter",
            expected_files=[
                "src/frontier_crypto_venues.py",
                "src/public_data_adapters/bitso_public.py",
                "tests/test_bitso_public_depth.py",
            ],
            tests_to_run=["pytest tests/test_bitso_public_depth.py -q"],
            proposed_change="Wire Bitso public order-book depth into the frontier scanner runtime.",
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn("src/frontier_crypto_adapter.py", preflight["target_files"])
        self.assertIn("src/frontier_data_quality.py", preflight["target_files"])
        self.assertIn("tests/test_frontier_data_quality.py", preflight["target_files"])
        self.assertFalse(preflight["invalid_targets"])
        self.assertFalse(preflight["test_issues"])
        self.assertEqual(preflight["quality_scorecard"]["preflight_reject_status"], None)

    def test_preflight_repairs_conceptual_paper_scoring_targets(self) -> None:
        payload = proposal(
            "",
            title="Quarantine decayed OKX basis mean-reversion paper signals",
            change_category="paper_scoring_logic",
            expected_files=[
                "paper signal scoring policy module that applies learned score_adjustment and activation/quarantine status",
                "paper order candidate policy module that can mark candidates shadow_filtered",
                "tests covering paper scoring policy for OKX perp_funding_basis variants",
            ],
            tests_to_run=["python -m pytest tests/test_paper_scoring.py -q"],
            proposed_change="Add a paper-only scoring guard for OKX basis mean-reversion candidates.",
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn("src/strategy_reliability.py", preflight["target_files"])
        self.assertIn("src/paper_order_router.py", preflight["target_files"])
        self.assertIn("tests/test_strategy_reliability.py", preflight["target_files"])
        self.assertFalse(preflight["invalid_targets"])
        self.assertFalse(preflight["test_issues"])
        self.assertEqual(preflight["quality_scorecard"]["target_path_status"], "repaired")
        self.assertEqual(preflight["quality_scorecard"]["preflight_reject_status"], None)

    def test_preflight_repairs_nonexistent_pytest_for_runtime_category(self) -> None:
        payload = proposal(
            "",
            change_category="scanner_expansion",
            expected_files=["src/frontier_data_quality.py"],
            tests_to_run=["pytest tests/test_market_expansion_quality.py -q"],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertFalse(preflight["test_issues"])
        self.assertTrue(preflight["test_repairs"])
        self.assertIn("tests/test_frontier_data_quality.py", preflight["parsed_tests"][0])

    def test_preflight_accepts_compileall_and_adds_canonical_tests(self) -> None:
        payload = proposal(
            "",
            change_category="read_only_route_intelligence",
            expected_files=["src/route_intelligence.py"],
            tests_to_run=["python -m compileall ."],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertFalse(preflight["test_issues"])
        self.assertIn([sys.executable, "-m", "compileall", "src"], preflight["parsed_tests"])
        self.assertIn(
            [sys.executable, "-m", "unittest", "tests/test_route_intelligence.py", "tests/test_route_resolver.py"],
            preflight["parsed_tests"],
        )

    def test_preflight_strips_quoted_unittest_discover_pattern(self) -> None:
        payload = proposal(
            "",
            change_category="parser_improvement",
            expected_files=["src/code_evolution.py"],
            tests_to_run=['python -m unittest discover -s tests -p "test_code_evolution.py"'],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertIn(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_code_evolution.py"],
            preflight["parsed_tests"],
        )

    def test_canonical_tests_repair_stale_model_tests(self) -> None:
        payload = proposal(
            "",
            change_category="paper_scoring_logic",
            expected_files=["src/strategy_reliability.py"],
            tests_to_run=["pytest tests/test_frontier_crypto_venues.py -q"],
        )

        preflight = code_evolution.preflight_proposal(payload, settings())

        self.assertFalse(preflight["test_issues"])
        self.assertTrue(preflight["test_repairs"])
        self.assertTrue(any("tests/test_strategy_reliability.py" in command for command in preflight["parsed_tests"]))

    def test_invalid_test_command_blocks_before_patch_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            try:
                rec = {
                    "recommendation_id": "rec-bad-test",
                    "title": "Bad test command",
                    "payload": proposal("", tests_to_run=["pytest tests/test_does_not_exist.py -q"]),
                }
                with mock.patch.object(code_evolution, "generate_patch_with_frontier_model") as generate:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(generate_patch_when_missing=True),
                        root=root,
                    )
                self.assertEqual(created[0]["status"], "rejected_preflight_invalid_tests")
                self.assertEqual(generate.call_count, 0)
            finally:
                conn.close()

    def test_scan_splits_empty_patch_and_quota_failures(self) -> None:
        empty = code_evolution.validate_and_scan(proposal("", expected_files=["README.md"]), "", settings())
        self.assertEqual(empty["decision"], "patch_generation_failed")

        quota = code_evolution.validate_and_scan(
            proposal("", expected_files=["README.md"]),
            "",
            settings(),
            patch_generation={"status": "fallback_error:429 insufficient_quota"},
        )
        self.assertEqual(quota["decision"], "patch_generation_unavailable_retry_later")
        self.assertIn("patch_generation_unavailable:quota_429", quota["reasons"])
        self.assertNotIn("no_changed_files", quota["reasons"])

    def test_patch_generation_budget_and_connection_are_retry_later(self) -> None:
        for status, reason in [
            ("agent_budget_guard:1+1>1", "patch_generation_unavailable:budget_guard"),
            ("fallback_error:Connection error.", "patch_generation_unavailable:connection_error"),
        ]:
            with self.subTest(status=status):
                safety = code_evolution.validate_and_scan(
                    proposal("", expected_files=["src/llm_bridge.py"], change_category="llm_prompt_state_packet"),
                    "",
                    settings(),
                    patch_generation={"status": status},
                )
                self.assertEqual(safety["decision"], "patch_generation_unavailable_retry_later")
                self.assertIn(reason, safety["reasons"])
                self.assertNotIn("no_changed_files", safety["reasons"])

    def test_model_non_diff_patch_is_invalid_patch_format(self) -> None:
        safety = code_evolution.validate_and_scan(
            proposal(
                "*** Begin Patch\n*** Update File: README.md\n@@\n+note\n*** End Patch\n",
                expected_files=["README.md"],
            ),
            "*** Begin Patch\n*** Update File: README.md\n@@\n+note\n*** End Patch\n",
            settings(),
            patch_generation={"status": "model_call:responses", "model_tier": "standard"},
        )

        self.assertEqual(safety["decision"], "invalid_patch_format")
        self.assertIn("invalid_patch_format", safety["reasons"])
        self.assertNotIn("no_changed_files", safety["reasons"])

    def test_process_repairs_non_diff_model_output_before_invalid_patch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_ledger = code_evolution.LEDGER_JSONL
            code_evolution.LEDGER_JSONL = pathlib.Path(tmp) / "evolution_ledger.jsonl"
            non_diff = "I would add a paper-only note to src/llm_bridge.py."
            repaired = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+paper-only note
"""
            try:
                rec = {
                    "recommendation_id": "rec-non-diff-repair",
                    "title": "Repair non-diff model output",
                    "payload": proposal(
                        "",
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                        tests_to_run=[],
                    ),
                }
                with mock.patch.object(
                    code_evolution,
                    "generate_patch_with_frontier_model",
                    return_value=(
                        non_diff,
                        {
                            "status": "model_call:responses",
                            "model_tier": "fast",
                            "returned_patch_format": "invalid_or_empty",
                        },
                    ),
                ), mock.patch.object(
                    code_evolution,
                    "repair_patch_with_frontier_model",
                    return_value=(
                        repaired,
                        {
                            "status": "model_call:responses",
                            "model_tier": "fast",
                            "returned_patch_format": "unified_diff",
                        },
                    ),
                ) as repair:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(generate_patch_when_missing=True, patch_repair_attempts=2),
                        root=root,
                    )
                row = storage.code_evolution_recent(conn)[0]
                updated_text = (root / "src" / "llm_bridge.py").read_text(encoding="utf-8")
            finally:
                code_evolution.LEDGER_JSONL = old_ledger
                conn.close()

        self.assertEqual(created[0]["status"], "workspace_applied_probation")
        self.assertEqual(repair.call_count, 1)
        self.assertIn("paper-only note", updated_text)
        self.assertEqual(row["status"], "workspace_applied_probation")
        self.assertEqual(row["changed_files"], ["src/llm_bridge.py"])
        self.assertEqual(row["safety"]["repair_history"][0]["previous_stage"], "invalid_patch_format")

    def test_unavailable_patch_generation_does_not_store_fallback_as_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            result = type(
                "Result",
                (),
                {
                    "text": json.dumps({"action": "propose_hunter_directive"}),
                    "status": "agent_budget_guard:1+1>1",
                    "model_name": "openai/gpt-5.4",
                    "model_tier": "standard",
                    "estimated_cost_usd": 0.0,
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                },
            )()
            try:
                rec = {
                    "recommendation_id": "rec-budget",
                    "title": "Budget unavailable patch",
                    "payload": proposal(
                        "",
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                    ),
                }
                with mock.patch.object(code_evolution, "complete", return_value=result):
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(generate_patch_when_missing=True, require_frontier_model=False),
                        root=root,
                    )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                conn.close()

        self.assertEqual(created[0]["status"], "patch_generation_unavailable_retry_later")
        self.assertIsNone(row["patch_text"])
        self.assertIn("patch_generation_unavailable:budget_guard", row["safety"]["reasons"])
        self.assertNotIn("no_changed_files", row["safety"]["reasons"])

    def test_patch_generation_preflight_budget_guard_skips_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            try:
                rec = {
                    "recommendation_id": "rec-preflight-budget",
                    "title": "Budget unavailable patch",
                    "payload": proposal(
                        "",
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                    ),
                }
                with mock.patch.object(
                    code_evolution,
                    "completion_preflight_status",
                    return_value={
                        "ok": False,
                        "status": "global_budget_guard:1+1>1",
                        "model_name": "openai/gpt-5.4",
                        "model_tier": "standard",
                        "prompt_tokens": 123,
                    },
                ):
                    with mock.patch.object(code_evolution, "complete") as complete:
                        created = code_evolution.process_code_change_recommendation(
                            conn,
                            rec,
                            settings(generate_patch_when_missing=True, require_frontier_model=False),
                            root=root,
                        )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                conn.close()

        self.assertEqual(complete.call_count, 0)
        self.assertEqual(created[0]["status"], "patch_generation_unavailable_retry_later")
        self.assertIn("patch_generation_unavailable:budget_guard", row["safety"]["reasons"])
        self.assertTrue(row["safety"]["patch_generation"]["preflight_skipped_model_call"])
        self.assertNotIn("no_changed_files", row["safety"]["reasons"])

    def test_temp_database_uses_temp_evolution_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            try:
                self.assertEqual(
                    code_evolution._ledger_path_for_connection(conn),
                    pathlib.Path(tmp) / "evolution_ledger.jsonl",
                )
            finally:
                conn.close()

    def test_report_normalizes_old_no_changed_fallbacks_and_invalid_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            try:
                storage.add_code_evolution_proposal(
                    conn,
                    "old-budget",
                    "rec-budget",
                    "build_planner",
                    "openai/gpt-5.4",
                    "standard",
                    None,
                    "Old budget fallback",
                    "report_dashboard",
                    90,
                    proposal(""),
                    {"report": "x"},
                )
                storage.update_code_evolution_proposal(
                    conn,
                    "old-budget",
                    status="no_changed_files",
                    patch_text=json.dumps({"action": "propose_hunter_directive"}),
                    safety={
                        "allowed": False,
                        "decision": "no_changed_files",
                        "reasons": ["no_changed_files"],
                        "patch_generation": {"status": "agent_budget_guard:1+1>1"},
                    },
                )
                storage.add_code_evolution_proposal(
                    conn,
                    "old-format",
                    "rec-format",
                    "build_planner",
                    "openai/gpt-5.4",
                    "standard",
                    None,
                    "Old non diff",
                    "report_dashboard",
                    90,
                    proposal(""),
                    {"report": "x"},
                )
                storage.update_code_evolution_proposal(
                    conn,
                    "old-format",
                    status="no_changed_files",
                    patch_text="*** Begin Patch\n*** Update File: README.md\n@@\n+note\n*** End Patch\n",
                    safety={"allowed": False, "decision": "no_changed_files", "reasons": ["no_changed_files"]},
                )

                normalized = code_evolution.normalize_code_evolution_statuses(conn)
                rows = {row["proposal_id"]: row for row in storage.code_evolution_recent(conn, limit=10)}
            finally:
                conn.close()

        self.assertEqual(normalized["patch_generation_unavailable_retry_later"], 1)
        self.assertEqual(normalized["invalid_patch_format"], 1)
        self.assertEqual(rows["old-budget"]["status"], "patch_generation_unavailable_retry_later")
        self.assertEqual(rows["old-format"]["status"], "invalid_patch_format")
        self.assertNotIn("no_changed_files", rows["old-budget"]["safety"]["reasons"])
        self.assertNotIn("no_changed_files", rows["old-format"]["safety"]["reasons"])

    def test_invalid_test_command_is_reported_before_sandbox(self) -> None:
        diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 hello
+paper-only note
"""
        payload = proposal(diff, tests_to_run=["pytest tests/test_does_not_exist.py -q; rm -rf runs"])
        preflight = code_evolution.preflight_proposal(payload, settings())

        safety = code_evolution.validate_and_scan(payload, diff, settings(), preflight=preflight)

        self.assertFalse(safety["allowed"])
        self.assertEqual(safety["decision"], "rejected_preflight_invalid_tests")
        self.assertIn("invalid_test_commands", safety["reasons"])

    def test_safety_scanner_allows_test_fixtures_with_live_and_borrow_strings(self) -> None:
        diff = """diff --git a/tests/test_safety_fixture.py b/tests/test_safety_fixture.py
--- /dev/null
+++ b/tests/test_safety_fixture.py
@@ -0,0 +1,5 @@
+def test_fixture_mentions_blocked_live_values():
+    payload = {"mode": "live", "spot_borrow": True}
+    text = "no trade, transfer, withdraw, or credential scope"
+    assert payload["mode"] == "live"
+    assert "withdraw" in text
"""
        safety = code_evolution.validate_and_scan(
            proposal(diff, expected_files=["tests/test_safety_fixture.py"], change_category="tests_fixtures"),
            diff,
            settings(),
        )

        self.assertTrue(safety["allowed"], safety)

    def test_safety_scanner_still_blocks_runtime_live_and_borrow_changes(self) -> None:
        diff = """diff --git a/src/strategy_reliability.py b/src/strategy_reliability.py
--- a/src/strategy_reliability.py
+++ b/src/strategy_reliability.py
@@ -1 +1,3 @@
 # strategy
+CONFIG = {"mode": "live"}
+ROUTE = {"spot_borrow": True}
"""
        safety = code_evolution.validate_and_scan(
            proposal(diff, expected_files=["src/strategy_reliability.py"], change_category="paper_scoring_logic"),
            diff,
            settings(),
        )

        self.assertFalse(safety["allowed"])
        self.assertIn("enables_live_mode", safety["reasons"])
        self.assertIn("enables_spot_borrow", safety["reasons"])

    def test_preflight_rejects_no_runtime_integration_before_model_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            try:
                rec = {
                    "recommendation_id": "rec-no-runtime",
                    "title": "Unused helper",
                    "payload": proposal(
                        "",
                        change_category="report_dashboard",
                        expected_files=["README.md"],
                        proposed_change="Add a standalone note that is not read by the runner.",
                    ),
                }
                with mock.patch.object(code_evolution, "generate_patch_with_frontier_model") as generate:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(generate_patch_when_missing=True),
                        root=root,
                    )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                conn.close()

        self.assertEqual(created[0]["status"], "rejected_preflight_no_runtime_integration")
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(
            row["safety"]["proposal_scorecard"]["runtime_integration_status"],
            "integration_claim_without_target",
        )

    def test_scan_rejects_orphan_source_helper_even_when_preflight_targeted_runtime_files(self) -> None:
        diff = """diff --git a/src/recommendation_schema.py b/src/recommendation_schema.py
new file mode 100644
--- /dev/null
+++ b/src/recommendation_schema.py
@@ -0,0 +1,2 @@
+def validate_recommendation_object(payload):
+    return isinstance(payload, dict)
diff --git a/tests/test_recommendation_schema.py b/tests/test_recommendation_schema.py
new file mode 100644
--- /dev/null
+++ b/tests/test_recommendation_schema.py
@@ -0,0 +1,2 @@
+def test_validate_recommendation_object():
+    assert True
"""
        payload = proposal(
            diff,
            change_category="evolution_loop_improvement",
            expected_files=["src/code_evolution.py", "tests/test_code_evolution.py"],
            proposed_change="Add a helper for recommendation schema validation.",
        )
        preflight = code_evolution.preflight_proposal(payload, settings())
        self.assertEqual(preflight["quality_scorecard"]["runtime_integration_status"], "integrated")

        safety = code_evolution.validate_and_scan(payload, diff, settings(), preflight=preflight)

        self.assertFalse(safety["allowed"])
        self.assertEqual(safety["decision"], "rejected_preflight_no_runtime_integration")
        self.assertIn("no_runtime_integration_target", safety["reasons"])
        self.assertEqual(
            safety["actual_runtime_integration_status"],
            "changed_source_without_runtime_wiring",
        )

    def test_patch_generation_timeout_is_classified(self) -> None:
        safety = code_evolution.validate_and_scan(
            proposal("", expected_files=["src/llm_bridge.py"], change_category="llm_prompt_state_packet"),
            "",
            settings(),
            patch_generation={"status": "fallback_error:request timed out"},
        )
        self.assertEqual(safety["decision"], "patch_generation_timeout")

    def test_report_normalizes_old_generic_blocked_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_json = code_evolution.REPORT_JSON
            old_md = code_evolution.REPORT_MD
            try:
                code_evolution.REPORT_JSON = pathlib.Path(tmp) / "evolution.json"
                code_evolution.REPORT_MD = pathlib.Path(tmp) / "evolution.md"
                storage.add_code_evolution_proposal(
                    conn,
                    "old-no-files",
                    "rec-old",
                    "build_planner",
                    "openai/gpt-5.6-sol",
                    "frontier",
                    "reason",
                    "Old no files",
                    "report_dashboard",
                    90,
                    proposal(""),
                    {"report": "x"},
                )
                storage.update_code_evolution_proposal(
                    conn,
                    "old-no-files",
                    status="blocked_human_review",
                    safety={"allowed": False, "decision": "blocked_human_review", "reasons": ["no_changed_files"]},
                )

                report = code_evolution.write_code_evolution_reports(conn, settings())
                row = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.REPORT_JSON = old_json
                code_evolution.REPORT_MD = old_md
                conn.close()

        self.assertEqual(report["normalized_statuses"], {"no_changed_files": 1})
        self.assertEqual(row["status"], "no_changed_files")

    def test_report_exposes_deferred_canary_and_failure_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_json = code_evolution.REPORT_JSON
            old_md = code_evolution.REPORT_MD
            try:
                code_evolution.REPORT_JSON = pathlib.Path(tmp) / "evolution.json"
                code_evolution.REPORT_MD = pathlib.Path(tmp) / "evolution.md"
                storage.add_code_evolution_proposal(
                    conn,
                    "proposal-deferred",
                    "rec-deferred",
                    "build_planner",
                    "openai/gpt-5.4-mini",
                    "fast",
                    None,
                    "Fast path",
                    "llm_prompt_state_packet",
                    90,
                    proposal("", expected_files=["src/llm_bridge.py"], change_category="llm_prompt_state_packet"),
                    {},
                )
                storage.update_code_evolution_proposal(
                    conn,
                    "proposal-deferred",
                    status="promoted",
                    safety={"reasons": []},
                    canary={"passed": True, "stage": "deferred_by_policy", "reason": "candidate_canary_disabled"},
                )
                report = code_evolution.write_code_evolution_reports(conn, settings())
            finally:
                code_evolution.REPORT_JSON = old_json
                code_evolution.REPORT_MD = old_md
                conn.close()

        summary = report["summary"]
        self.assertEqual(summary["canary_stage_counts"]["deferred_by_policy"], 1)
        self.assertIn("failure_benchmark", summary)

    def test_duplicate_structural_failures_are_suppressed_before_model_spend(self) -> None:
        payload = proposal(
            "",
            title="Paper-quarantine OKX basis mean reversion",
            change_category="paper_scoring_logic",
            expected_files=["src/strategy_reliability.py"],
            proposed_change="Quarantine repeatedly bad OKX basis mean-reversion paper signals.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "strategy_reliability.py").write_text("# strategy\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_strategy_reliability.py").write_text("import unittest\n", encoding="utf-8")
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            preflight = code_evolution.preflight_proposal(payload, settings(), root=root)
            try:
                for idx in range(3):
                    pid = f"old-failed-{idx}"
                    storage.add_code_evolution_proposal(
                        conn,
                        pid,
                        f"rec-{idx}",
                        "red_team",
                        "openai/gpt-5.4-mini",
                        "fast",
                        None,
                        payload["title"],
                        "paper_scoring_logic",
                        90,
                        payload,
                        {"evidence": "test"},
                    )
                    storage.update_code_evolution_proposal(
                        conn,
                        pid,
                        status="discarded_test_failure",
                        safety={"preflight": preflight, "reasons": ["test_failure"]},
                    )
                rec = {"recommendation_id": "rec-new", "title": payload["title"], "payload": payload}
                with mock.patch.object(code_evolution, "generate_patch_with_frontier_model") as generate:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(
                            generate_patch_when_missing=True,
                            duplicate_failure_suppression_after=3,
                        ),
                        root=root,
                    )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                conn.close()

        self.assertEqual(created[0]["status"], "rejected_duplicate_recent_failure")
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(row["status"], "rejected_duplicate_recent_failure")
        self.assertIn("duplicate_recent_structural_failure", row["safety"]["reasons"])

    def test_process_safe_patch_in_temp_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_ledger = code_evolution.LEDGER_JSONL
            code_evolution.LEDGER_JSONL = pathlib.Path(tmp) / "evolution_ledger.jsonl"
            diff = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+paper-only note
"""
            try:
                rec = {
                    "recommendation_id": "rec-1",
                    "title": "Improve report wording",
                    "payload": proposal(
                        diff,
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                    ),
                }
                created = code_evolution.process_code_change_recommendation(conn, rec, settings(), root=root)
                self.assertEqual(created[0]["status"], "workspace_applied_probation")
                self.assertIn("paper-only note", (root / "src" / "llm_bridge.py").read_text(encoding="utf-8"))
                rows = storage.code_evolution_recent(conn)
            finally:
                code_evolution.LEDGER_JSONL = old_ledger
                conn.close()
        self.assertEqual(rows[0]["status"], "workspace_applied_probation")
        self.assertEqual(rows[0]["changed_files"], ["src/llm_bridge.py"])

    @unittest.skipUnless(shutil.which("git"), "git executable is required")
    def test_git_release_promotes_after_sandbox_when_canary_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp) / "repo"
            app = repo / "agentic_trading_swarm_mvp"
            (app / "src").mkdir(parents=True)
            (app / "tests").mkdir()
            (app / "config").mkdir()
            (app / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "codex@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "tag", "champion/test"], cwd=repo, check=True)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs = code_evolution.RUNS_DIR
            old_ledger = code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"
            diff = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+FAST_PROMOTION_MARKER = True
"""
            try:
                rec = {
                    "recommendation_id": "rec-fast-release",
                    "title": "Fast release",
                    "payload": proposal(
                        diff,
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                        tests_to_run=[],
                    ),
                }
                created = code_evolution.process_code_change_recommendation(
                    conn,
                    rec,
                    settings(
                        git_release_enabled=True,
                        run_candidate_canary=False,
                        promote_candidate_after_canary=True,
                        run_full_regression=False,
                        release_worktree_dir=str(pathlib.Path(tmp) / "worktrees"),
                    ),
                    root=app,
                )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.RUNS_DIR = old_runs
                code_evolution.LEDGER_JSONL = old_ledger
                conn.close()
            updated_text = (app / "src" / "llm_bridge.py").read_text(encoding="utf-8")
            latest = subprocess.run(
                ["git", "rev-parse", "champion/latest"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        self.assertEqual(created[0]["status"], "promoted")
        self.assertEqual(row["status"], "promoted")
        self.assertEqual(row["canary"]["stage"], "deferred_by_policy")
        self.assertTrue(row["candidate_commit"])
        self.assertIn("FAST_PROMOTION_MARKER", updated_text)
        self.assertEqual(latest, row["candidate_commit"])

    def test_pytest_paths_are_translated_to_safe_unittest_commands(self) -> None:
        payload = proposal(
            "",
            tests_to_run=[
                "pytest tests/test_frontier_crypto_adapter.py -q",
                "python -m pytest tests/test_frontier_data_quality.py::QualityMathTests -q",
            ],
        )

        commands = code_evolution._test_commands(payload, {"run_full_regression": False})

        self.assertEqual(
            commands,
            [
                [sys.executable, "-m", "unittest", "tests/test_frontier_crypto_adapter.py"],
                [sys.executable, "-m", "unittest", "tests/test_frontier_data_quality.py"],
            ],
        )

    def test_process_repairs_malformed_patch_before_discarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_ledger = code_evolution.LEDGER_JSONL
            code_evolution.LEDGER_JSONL = pathlib.Path(tmp) / "evolution_ledger.jsonl"
            malformed = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@?
 # bridge
+paper-only note
"""
            repaired = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+paper-only note
"""
            try:
                rec = {
                    "recommendation_id": "rec-repair",
                    "title": "Repair malformed patch",
                    "payload": proposal(
                        "",
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                        tests_to_run=[],
                    ),
                }
                with mock.patch.object(
                    code_evolution,
                    "generate_patch_with_frontier_model",
                    return_value=(malformed, {"status": "model_call:responses"}),
                ), mock.patch.object(
                    code_evolution,
                    "repair_patch_with_frontier_model",
                    return_value=(repaired, {"status": "model_call:responses"}),
                ) as repair:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(generate_patch_when_missing=True, patch_repair_attempts=3),
                        root=root,
                    )
                self.assertEqual(created[0]["status"], "workspace_applied_probation")
                self.assertEqual(repair.call_count, 1)
                self.assertIn("paper-only note", (root / "src" / "llm_bridge.py").read_text(encoding="utf-8"))
            finally:
                code_evolution.LEDGER_JSONL = old_ledger
                conn.close()

    def test_local_context_apply_handles_non_git_hunk_suffix_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "llm_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "config").mkdir()
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_ledger = code_evolution.LEDGER_JSONL
            code_evolution.LEDGER_JSONL = pathlib.Path(tmp) / "evolution_ledger.jsonl"
            malformed_but_parseable = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@?
 # bridge
+paper-only note
"""
            try:
                rec = {
                    "recommendation_id": "rec-local-context",
                    "title": "Apply with local context matcher",
                    "payload": proposal(
                        malformed_but_parseable,
                        expected_files=["src/llm_bridge.py"],
                        change_category="llm_prompt_state_packet",
                        tests_to_run=[],
                    ),
                }
                with mock.patch.object(code_evolution, "repair_patch_with_frontier_model") as repair:
                    created = code_evolution.process_code_change_recommendation(
                        conn,
                        rec,
                        settings(patch_repair_attempts=3, local_context_patch_apply=True),
                        root=root,
                    )
                self.assertEqual(created[0]["status"], "workspace_applied_probation")
                self.assertEqual(repair.call_count, 0)
                self.assertIn("paper-only note", (root / "src" / "llm_bridge.py").read_text(encoding="utf-8"))
                rows = storage.code_evolution_recent(conn)
                self.assertEqual(rows[0]["status"], "workspace_applied_probation")
            finally:
                code_evolution.LEDGER_JSONL = old_ledger
                conn.close()

    def test_local_context_apply_directly_handles_non_git_hunk_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -99 +99,2 @@?
 hello
+paper-only note
"""
            result = code_evolution._apply_unified_diff_by_context(diff, root)
            updated = (root / "README.md").read_text(encoding="utf-8")

        self.assertTrue(result["applied"], result)
        self.assertIn("paper-only note", updated)

    def test_local_context_apply_rejects_unmatched_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            root.mkdir()
            (root / "README.md").write_text("hello\n", encoding="utf-8")
            diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -9 +9,2 @@?
 missing line
+paper-only note
"""
            result = code_evolution._apply_unified_diff_by_context(diff, root)

        self.assertFalse(result["applied"])
        self.assertEqual(result["stage"], "internal_context_match")


class CodeEvolutionBridgeTests(unittest.TestCase):
    def test_llm_bridge_accepts_propose_code_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_inbox = llm_bridge.INBOX
            old_processed = llm_bridge.PROCESSED
            try:
                llm_bridge.INBOX = pathlib.Path(tmp) / "inbox.jsonl"
                llm_bridge.PROCESSED = pathlib.Path(tmp) / "processed.jsonl"
                llm_bridge.INBOX.write_text(
                    json.dumps(
                        proposal(
                            "diff --git a/README.md b/README.md\n"
                            "--- a/README.md\n"
                            "+++ b/README.md\n"
                            "@@ -1 +1,2 @@\n"
                            " hello\n"
                            "+paper-only note\n"
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                accepted = llm_bridge.ingest_llm_recommendations(
                    conn,
                    {
                        "llm_bridge": {
                            "enabled": True,
                            "ingest_recommendations": True,
                            "max_recommendations_per_loop": 20,
                            "allowed_actions": ["propose_code_change"],
                        }
                    },
                )
                rows = storage.llm_recommendations_for_auto_execution(conn)
            finally:
                llm_bridge.INBOX = old_inbox
                llm_bridge.PROCESSED = old_processed
                conn.close()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rows[0]["action"], "propose_code_change")


if __name__ == "__main__":
    unittest.main()

