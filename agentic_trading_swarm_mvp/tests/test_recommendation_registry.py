from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recommendation_registry import (
    backfill_open_artifacts,
    bind_artifact,
    claim_topic,
    reconcile_deployed_artifacts,
    registry_summary,
)
from storage import init_db


class RecommendationRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_cross_action_paraphrase_reuses_canonical_topic(self) -> None:
        first = claim_topic(
            self.conn,
            payload={
                "market_key": "BITKUB|THB",
                "title": "Add verified Bitkub depth coverage",
                "proposed_change": "Wire public order-book depth into normal frontier quality scoring.",
                "recommended_next_action": "adapter_spec",
            },
            topic_type="market_adapter",
            priority=90,
            source_ref="llm:one",
        )
        bind_artifact(self.conn, first.topic_key, "adapter_specs", 17)
        second = claim_topic(
            self.conn,
            payload={
                "market_key": "BITKUB|THB",
                "title": "Implement Bitkub public depth",
                "proposed_change": "Integrate public order-book depth with frontier quality scoring.",
                "recommended_next_action": "propose_code_change",
            },
            topic_type="code_change",
            priority=95,
            source_ref="llm:two",
        )
        self.assertTrue(second.duplicate)
        self.assertEqual("adapter_specs", second.canonical_table)
        self.assertEqual("17", second.canonical_row_id)
        self.assertEqual(1, registry_summary(self.conn)["duplicates_suppressed"])

    def test_different_market_does_not_deduplicate(self) -> None:
        one = claim_topic(
            self.conn,
            payload={"market_key": "BITKUB|THB", "title": "Add verified depth"},
            topic_type="market_adapter",
            priority=80,
        )
        two = claim_topic(
            self.conn,
            payload={"market_key": "BYBIT|USDT", "title": "Add verified depth"},
            topic_type="market_adapter",
            priority=80,
        )
        self.assertNotEqual(one.topic_key, two.topic_key)
        self.assertTrue(two.created)

    def test_material_stage_change_reopens_implemented_topic(self) -> None:
        one = claim_topic(
            self.conn,
            payload={
                "market_key": "JSE|NPN",
                "title": "Advance market admission",
                "admission_stage": "priceable",
            },
            topic_type="market_admission",
            priority=90,
        )
        self.conn.execute(
            "update recommendation_topics set status = 'implemented_market_admission' where topic_key = ?",
            (one.topic_key,),
        )
        two = claim_topic(
            self.conn,
            payload={
                "market_key": "JSE|NPN",
                "title": "Advance market admission",
                "admission_stage": "quality_verified",
            },
            topic_type="market_admission",
            priority=90,
        )
        self.assertTrue(two.reopened)

    def test_backfill_preserves_one_canonical_row_and_supersedes_duplicate(self) -> None:
        self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 90, ?, ?, 'open')",
            ("Add Bitkub depth quality", "Wire Bitkub public depth into frontier quality scoring."),
        )
        self.conn.execute(
            "insert into adapter_specs (created_at, market_key, priority, title, status, spec_json, evidence_json) values ('now', ?, 90, ?, 'open', ?, '{}')",
            ("BITKUB|THB", "Implement Bitkub depth quality", '{"proposed_change":"Wire Bitkub public depth into frontier quality scoring."}'),
        )
        result = backfill_open_artifacts(self.conn)
        self.assertEqual(2, result["registered"] + result["superseded"])
        self.assertEqual(2, self.conn.execute("select count(*) from recommendation_topic_sources").fetchone()[0])

    def test_reconcile_closes_only_narrow_deployed_capability_matches(self) -> None:
        self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 1, 'marker', '', 'implemented_typed_recommendation_contract')"
        )
        matching = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 90, ?, ?, 'open') returning id",
            (
                "Enforce complete JSON recommendation output for execution_route_hunter",
                "Validate one schema-complete recommendation object.",
            ),
        ).fetchone()[0]
        distinct = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 90, ?, ?, 'open') returning id",
            (
                "Improve execution route reasoning",
                "Compare venue latency and fee evidence before ranking routes.",
            ),
        ).fetchone()[0]

        result = reconcile_deployed_artifacts(self.conn)

        self.assertEqual(1, result["closed_count"])
        self.assertEqual(1, result["reconciled_total_count"])
        self.assertEqual(
            "superseded_by_implemented_typed_recommendation_contract",
            self.conn.execute("select status from improvement_tasks where id = ?", (matching,)).fetchone()[0],
        )
        self.assertEqual(
            "open",
            self.conn.execute("select status from improvement_tasks where id = ?", (distinct,)).fetchone()[0],
        )

    def test_reconcile_requires_an_existing_implemented_marker(self) -> None:
        task_id = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 90, ?, ?, 'open') returning id",
            (
                "Enforce complete JSON recommendation output for execution_route_hunter",
                "Validate one schema-complete recommendation object.",
            ),
        ).fetchone()[0]

        result = reconcile_deployed_artifacts(self.conn)

        self.assertEqual(0, result["closed_count"])
        self.assertEqual(0, result["reconciled_total_count"])
        self.assertEqual(
            "open",
            self.conn.execute("select status from improvement_tasks where id = ?", (task_id,)).fetchone()[0],
        )

    def test_reconcile_closes_deployed_regional_fx_normalization(self) -> None:
        self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 1, 'marker', '', 'implemented_regional_fx_frontier_prediction_pack')"
        )
        task_id = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 91, ?, '', 'open') returning id",
            ("LLM: Paper FX-normalization adapter for frontier fiat-quoted crypto",),
        ).fetchone()[0]

        result = reconcile_deployed_artifacts(self.conn)

        self.assertEqual(1, result["closed_count"])
        self.assertEqual(
            "superseded_by_implemented_regional_fx_normalization",
            self.conn.execute("select status from improvement_tasks where id = ?", (task_id,)).fetchone()[0],
        )

    def test_reconcile_closes_task_with_exact_promoted_code_title(self) -> None:
        task_id = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 93, ?, '', 'open') returning id",
            ("LLM: Add context-inheritance guard for Strategy Lab variants",),
        ).fetchone()[0]
        self.conn.execute(
            """
            insert into code_evolution_proposals (
                proposal_id, created_at, updated_at, title, category, priority, status,
                payload_json, evidence_json, changed_files_json, safety_json, tests_json,
                evaluation_json, candidate_commit
            ) values ('code_evolution:test', 'now', 'now', ?, 'paper_scoring_logic', 93,
                      'promoted', '{}', '{}', '[]', '{}', '{}', '{}', 'abc123')
            """,
            ("Add context-inheritance guard for Strategy Lab variants",),
        )

        result = reconcile_deployed_artifacts(self.conn)

        self.assertEqual(1, result["closed_count"])
        self.assertEqual(
            "implemented_by_promoted_code_evolution",
            self.conn.execute("select status from improvement_tasks where id = ?", (task_id,)).fetchone()[0],
        )
        self.assertEqual("abc123", result["closed"][0]["implementation_commit"])

    def test_reconcile_does_not_close_paraphrase_without_exact_promoted_title(self) -> None:
        task_id = self.conn.execute(
            "insert into improvement_tasks (created_at, priority, title, rationale, status) values ('now', 93, ?, '', 'open') returning id",
            ("Add broader context inheritance to Strategy Lab",),
        ).fetchone()[0]
        self.conn.execute(
            """
            insert into code_evolution_proposals (
                proposal_id, created_at, updated_at, title, category, priority, status,
                payload_json, evidence_json, changed_files_json, safety_json, tests_json,
                evaluation_json, candidate_commit
            ) values ('code_evolution:test', 'now', 'now', ?, 'paper_scoring_logic', 93,
                      'promoted', '{}', '{}', '[]', '{}', '{}', '{}', 'abc123')
            """,
            ("Add context-inheritance guard for Strategy Lab variants",),
        )

        result = reconcile_deployed_artifacts(self.conn)

        self.assertEqual(0, result["closed_count"])
        self.assertEqual(
            "open",
            self.conn.execute("select status from improvement_tasks where id = ?", (task_id,)).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
