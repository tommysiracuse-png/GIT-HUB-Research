from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recommendation_registry import backfill_open_artifacts, bind_artifact, claim_topic, registry_summary
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


if __name__ == "__main__":
    unittest.main()
