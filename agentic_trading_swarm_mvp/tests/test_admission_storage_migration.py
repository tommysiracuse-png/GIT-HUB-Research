import pathlib
import json
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from storage import (
    consume_bounded_paper_queue_claim,
    init_db,
    open_paper_trade,
    save_frontier_paper_shadow_observation,
)
from paper_admission_queue import (
    enqueue_paper_admission_candidates,
    reconcile_paper_admission_queue,
    select_paper_admission_candidates,
)


class AdmissionStorageMigrationTests(unittest.TestCase):
    def test_legacy_duplicate_bounded_artifacts_are_preserved_and_guarded(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = {
            "market_admission": {
                "enabled": True,
                "paper_queue_enabled": True,
                "paper_queue": {
                    "max_active": 200,
                    "max_enqueue_per_cycle": 30,
                    "max_select_per_cycle": 30,
                },
            }
        }
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "asset_class": "crypto",
            "market_type": "perp",
            "market_surface": "perp_funding_basis",
            "direction": "short_perp_long_spot",
            "trade_type": "perp_funding_basis",
            "route_status": "standard",
            "quality_status": "verified",
            "freshness_state": "fresh",
            "data_status": "reachable",
            "execution_feasibility": {"status": "standard"},
            "last": 100.0,
            "score": 80.0,
        }
        try:
            enqueue_paper_admission_candidates(conn, settings, [candidate])
            claimed = select_paper_admission_candidates(conn, settings)[0]
            self.assertTrue(
                consume_bounded_paper_queue_claim(conn, claimed, settings)
            )
            for trigger in (
                "trg_bounded_paper_order_guard_insert",
                "trg_bounded_paper_order_bind_insert",
                "trg_bounded_paper_trade_guard_insert",
                "trg_bounded_paper_trade_bind_insert",
            ):
                conn.execute(f"drop trigger {trigger}")
            for index in (
                "idx_bounded_paper_filled_order_episode_unique",
                "idx_bounded_paper_trade_episode_unique",
                "idx_bounded_paper_trade_order_unique",
            ):
                conn.execute(f"drop index if exists {index}")
            admission_key = claimed["admission_key"]
            episode_id = claimed["episode_id"]
            order_ids = []
            for suffix in ("a", "b"):
                cursor = conn.execute(
                    """
                    insert into execution_orders(
                        created_at,mode,route_id,venue,inst_id,direction,trade_type,
                        status,notional_usd,order_json,candidate_json,review_json,
                        admission_key,admission_episode_id
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"2026-08-07T12:00:0{len(order_ids)}+00:00",
                        "paper",
                        f"legacy-{suffix}",
                        claimed["venue"],
                        claimed["inst_id"],
                        claimed["direction"],
                        claimed["trade_type"],
                        "paper_filled",
                        100.0,
                        json.dumps({"legacy": suffix}),
                        json.dumps(claimed, sort_keys=True),
                        "{}",
                        admission_key,
                        episode_id,
                    ),
                )
                order_ids.append(int(cursor.lastrowid))
            for order_id in order_ids:
                conn.execute(
                    """
                    insert into paper_trades(
                        opened_at,venue,inst_id,direction,trade_type,signal_key,
                        base_score,learned_score,entry,status,thesis,candidate_json,
                        review_json,execution_order_id,admission_key,
                        admission_episode_id
                    ) values(?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?)
                    """,
                    (
                        "2026-08-07T12:01:00+00:00",
                        claimed["venue"],
                        claimed["inst_id"],
                        claimed["direction"],
                        claimed["trade_type"],
                        "legacy",
                        80.0,
                        80.0,
                        100.0,
                        "legacy duplicate",
                        json.dumps(claimed, sort_keys=True),
                        "{}",
                        order_id,
                        admission_key,
                        episode_id,
                    ),
                )
            conn.commit()

            init_db(conn)

            queue_row = conn.execute(
                "select execution_order_id,paper_trade_id from paper_admission_queue"
            ).fetchone()
            self.assertIsNone(queue_row["execution_order_id"])
            self.assertIsNone(queue_row["paper_trade_id"])
            self.assertEqual(2, conn.execute("select count(*) from execution_orders").fetchone()[0])
            self.assertEqual(2, conn.execute("select count(*) from paper_trades").fetchone()[0])
            indexes = {
                row["name"]
                for table in ("execution_orders", "paper_trades")
                for row in conn.execute(f"pragma index_list({table})")
            }
            self.assertNotIn("idx_bounded_paper_filled_order_episode_unique", indexes)
            self.assertNotIn("idx_bounded_paper_trade_episode_unique", indexes)
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "bounded_paper_execution_order_already_exists",
            ):
                conn.execute(
                    """
                    insert into execution_orders(
                        created_at,mode,route_id,venue,inst_id,direction,trade_type,
                        status,notional_usd,order_json,candidate_json,review_json,
                        admission_key,admission_episode_id
                    ) values('2026-08-07T12:02:00+00:00','paper','new','OKX',
                             'BTC-USDT-SWAP','short_perp_long_spot',
                             'perp_funding_basis','paper_filled',100,'{}',?,'{}',?,?)
                    """,
                    (json.dumps(claimed, sort_keys=True), admission_key, episode_id),
                )
            conn.rollback()
            reconciliation = reconcile_paper_admission_queue(conn, settings)
            queue_row = conn.execute(
                "select execution_order_id,paper_trade_id,last_reason from paper_admission_queue"
            ).fetchone()
            self.assertEqual(
                1,
                reconciliation["by_decision"]["execution_order_lineage_ambiguous"],
            )
            self.assertIsNone(queue_row["execution_order_id"])
            self.assertIsNone(queue_row["paper_trade_id"])
            self.assertEqual(
                "execution_order_lineage_ambiguous", queue_row["last_reason"]
            )
        finally:
            conn.close()

    def test_legacy_outcome_tables_gain_and_backfill_admission_identity(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        init_db(conn)
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "direction": "short_perp_long_spot",
            "trade_type": "perp_funding_basis",
            "last": 100.0,
            "score": 80.0,
            "execution_feasibility": {"status": "standard"},
            "admission_key": "admission-legacy",
            "admission_episode_id": "episode-legacy",
        }
        review = {
            "decision": "approve_paper_trade",
            "learned_score": 80.0,
            "hard_blocks": [],
        }
        try:
            trade_id = open_paper_trade(conn, candidate, review)
            shadow_id = save_frontier_paper_shadow_observation(conn, candidate, review)
            conn.execute("drop index if exists idx_paper_outcomes_admission")
            conn.execute("drop index if exists idx_frontier_shadow_outcomes_admission")
            conn.execute("drop index if exists idx_outcomes_trade")
            conn.execute("drop index if exists idx_frontier_shadow_outcomes_due")
            conn.execute("drop table paper_trade_outcomes")
            conn.execute("drop table frontier_paper_shadow_outcomes")
            conn.executescript(
                """
                create table paper_trade_outcomes (
                    id integer primary key autoincrement,
                    trade_id integer not null,
                    horizon_minutes integer not null,
                    measured_at text not null,
                    price real not null,
                    pnl_bps real not null,
                    context_json text not null,
                    unique(trade_id,horizon_minutes)
                );
                create table frontier_paper_shadow_outcomes (
                    id integer primary key autoincrement,
                    observation_id integer not null,
                    horizon_minutes integer not null,
                    measured_at text not null,
                    price real,
                    pnl_bps real,
                    context_json text not null,
                    target_at text not null,
                    observed_at text,
                    delay_seconds real,
                    measurement_status text not null,
                    price_source text,
                    unique(observation_id,horizon_minutes)
                );
                """
            )
            conn.execute(
                """
                insert into paper_trade_outcomes(
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json
                ) values(?,60,'2026-08-07T12:00:00Z',101,10,'{}')
                """,
                (trade_id,),
            )
            conn.execute(
                """
                insert into frontier_paper_shadow_outcomes(
                    observation_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    target_at,observed_at,delay_seconds,measurement_status,price_source
                ) values(?,60,'2026-08-07T12:00:00Z',101,10,'{}',
                         '2026-08-07T12:00:00Z','2026-08-07T12:00:00Z',0,'valid','test')
                """,
                (shadow_id,),
            )
            conn.commit()

            init_db(conn)

            paper_columns = {
                row["name"] for row in conn.execute("pragma table_info(paper_trade_outcomes)")
            }
            shadow_columns = {
                row["name"]
                for row in conn.execute("pragma table_info(frontier_paper_shadow_outcomes)")
            }
            self.assertTrue({"admission_key", "admission_episode_id"}.issubset(paper_columns))
            self.assertTrue({"admission_key", "admission_episode_id"}.issubset(shadow_columns))
            paper = conn.execute(
                "select admission_key,admission_episode_id from paper_trade_outcomes"
            ).fetchone()
            shadow = conn.execute(
                "select admission_key,admission_episode_id from frontier_paper_shadow_outcomes"
            ).fetchone()
            self.assertEqual(("admission-legacy", "episode-legacy"), tuple(paper))
            self.assertEqual(("admission-legacy", "episode-legacy"), tuple(shadow))
            indexes = {
                row["name"]
                for table in ("paper_trade_outcomes", "frontier_paper_shadow_outcomes")
                for row in conn.execute(f"pragma index_list({table})")
            }
            self.assertIn("idx_paper_outcomes_admission", indexes)
            self.assertIn("idx_frontier_shadow_outcomes_admission", indexes)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
