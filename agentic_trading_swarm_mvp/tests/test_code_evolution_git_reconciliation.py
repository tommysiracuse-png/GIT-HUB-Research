from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import code_evolution
from storage import add_code_evolution_proposal, code_evolution_recent, init_db, update_code_evolution_proposal


def git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class CodeEvolutionGitReconciliationTests(unittest.TestCase):
    def test_candidate_in_head_ancestry_becomes_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            git(root, "init")
            git(root, "config", "user.email", "tests@example.com")
            git(root, "config", "user.name", "Tests")
            (root / "one.txt").write_text("one", encoding="utf-8")
            git(root, "add", "one.txt")
            git(root, "commit", "-m", "candidate")
            candidate = git(root, "rev-parse", "HEAD")
            (root / "two.txt").write_text("two", encoding="utf-8")
            git(root, "add", "two.txt")
            git(root, "commit", "-m", "later")

            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            add_code_evolution_proposal(
                conn, "proposal:test", None, "builder", "model", "standard", None,
                "Candidate", "runtime_pipeline_integration", 90, {}, {},
            )
            update_code_evolution_proposal(
                conn, "proposal:test", status="discarded_test_failure", candidate_commit=candidate,
            )
            result = code_evolution.reconcile_code_evolution_git_statuses(conn, root=root)
            row = code_evolution_recent(conn, limit=1)[0]
            conn.close()
            self.assertEqual(1, result["promoted_from_git_ancestry"])
            self.assertEqual("promoted", row["status"])

    def test_candidate_commit_is_recovered_from_exact_autonomous_commit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            git(root, "init")
            git(root, "config", "user.email", "tests@example.com")
            git(root, "config", "user.name", "Tests")
            (root / "one.txt").write_text("one", encoding="utf-8")
            git(root, "add", "one.txt")
            git(root, "commit", "-m", "baseline")

            proposal_id = "code_evolution:interrupted"
            (root / "two.txt").write_text("two", encoding="utf-8")
            git(root, "add", "two.txt")
            git(root, "commit", "-m", f"Autonomous candidate {proposal_id}")
            candidate = git(root, "rev-parse", "HEAD")

            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            init_db(conn)
            add_code_evolution_proposal(
                conn, proposal_id, None, "builder", "model", "standard", None,
                "Interrupted candidate", "runtime_pipeline_integration", 90, {}, {},
            )

            result = code_evolution.reconcile_code_evolution_git_statuses(conn, root=root)
            row = code_evolution_recent(conn, limit=1)[0]
            conn.close()

            self.assertEqual(1, result["candidate_commit_recovered_from_git"])
            self.assertEqual(1, result["promoted_from_git_ancestry"])
            self.assertEqual("promoted", row["status"])
            self.assertEqual(candidate, row["candidate_commit"])
            self.assertEqual(["two.txt"], row["changed_files"])


if __name__ == "__main__":
    unittest.main()
