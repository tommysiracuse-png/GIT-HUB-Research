from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.worktree import (  # noqa: E402
    cleanup_worktree,
    commit_candidate,
    create_candidate_worktree,
    promote_candidate,
    release_preflight,
    run_git,
    update_champion_latest,
)


def git_available() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(git_available(), "git executable is required")
class EvolutionReleaseTests(unittest.TestCase):
    def _repo(self, tmp: str) -> tuple[pathlib.Path, pathlib.Path]:
        root = pathlib.Path(tmp) / "repo"
        app = root / "agentic_trading_swarm_mvp"
        (app / "src").mkdir(parents=True)
        (app / "config").mkdir()
        (app / "src" / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "codex@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "tag", "champion/test"], cwd=root, check=True)
        return root, app

    def test_release_preflight_requires_clean_source_tree_and_champion_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, app = self._repo(tmp)

            clean = release_preflight(app)
            self.assertTrue(clean["ok"], clean)
            self.assertEqual(clean["champion_tag"], "champion/test")

            (app / "src" / "dirty.py").write_text("DIRTY = True\n", encoding="utf-8")
            dirty = release_preflight(app)
            self.assertFalse(dirty["ok"])
            self.assertEqual(dirty["reason"], "dirty_source_tree")
            self.assertIn("agentic_trading_swarm_mvp/src/dirty.py", dirty["dirty_paths"])

    def test_candidate_worktree_commits_metadata_without_touching_main_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, app = self._repo(tmp)
            release, created = create_candidate_worktree(app, "proposal:abc-123", base_dir=pathlib.Path(tmp) / "worktrees")
            self.assertIsNotNone(release, created)
            assert release is not None
            candidate_app = pathlib.Path(release.app_worktree_path)
            (candidate_app / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")

            release, committed = commit_candidate(release, "candidate")

            self.assertTrue(committed["ok"], committed)
            self.assertEqual(release.status, "candidate_committed")
            self.assertTrue(release.candidate_commit)
            self.assertEqual((app / "src" / "demo.py").read_text(encoding="utf-8"), "VALUE = 1\n")

            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)
            branch_check = run_git(["rev-parse", "--verify", release.branch_name], app)
            self.assertEqual(branch_check["returncode"], 0)

    def test_locked_stale_worktree_uses_retry_location_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, app = self._repo(tmp)
            base_dir = pathlib.Path(tmp) / "worktrees"
            stale = base_dir / "locked"
            stale.mkdir(parents=True)
            (stale / "locked.py").write_text("LOCKED = True\n", encoding="utf-8")

            with mock.patch("evolution.worktree.shutil.rmtree", side_effect=PermissionError("file is in use")):
                release, created = create_candidate_worktree(app, "proposal:locked", base_dir=base_dir)

            self.assertIsNotNone(release, created)
            assert release is not None
            self.assertTrue(created["cleanup"]["fallback_used"])
            self.assertIn("-retry-1", release.worktree_path)
            self.assertTrue(pathlib.Path(release.worktree_path).exists())
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)

    def test_cherry_pick_promotion_tags_actual_main_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self._repo(tmp)
            release, created = create_candidate_worktree(app, "proposal:diverged", base_dir=pathlib.Path(tmp) / "worktrees")
            self.assertIsNotNone(release, created)
            assert release is not None
            candidate_app = pathlib.Path(release.app_worktree_path)
            (candidate_app / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")
            release, committed = commit_candidate(release, "candidate")
            self.assertTrue(committed["ok"], committed)
            source_candidate = release.candidate_commit

            (app / "src" / "main_only.py").write_text("MAIN_ONLY = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "diverge main"], cwd=root, check=True, capture_output=True, text=True)

            release, promotion = promote_candidate(release, app)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            champion = subprocess.run(
                ["git", "rev-parse", "champion/latest"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()

            self.assertTrue(promotion["ok"], promotion)
            self.assertEqual(promotion["promotion_method"], "disjoint_cherry_pick")
            self.assertNotEqual(source_candidate, head)
            self.assertEqual(head, promotion["promoted_commit"])
            self.assertEqual(head, champion)
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)

    def test_fast_forward_promotion_preserves_current_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self._repo(tmp)
            release, created = create_candidate_worktree(app, "proposal:fast-forward", base_dir=pathlib.Path(tmp) / "worktrees")
            self.assertIsNotNone(release, created)
            assert release is not None
            (pathlib.Path(release.app_worktree_path) / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")
            release, committed = commit_candidate(release, "candidate")
            self.assertTrue(committed["ok"], committed)

            release, promotion = promote_candidate(release, app)

            self.assertTrue(promotion["ok"], promotion)
            self.assertEqual(promotion["promotion_method"], "fast_forward")
            current = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(current, release.candidate_commit)
            champion = subprocess.run(
                ["git", "rev-parse", "champion/latest"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(current, champion)
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)

    def test_failed_disjoint_cherry_pick_aborts_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self._repo(tmp)
            release, created = create_candidate_worktree(
                app, "proposal:directory-file-conflict", base_dir=pathlib.Path(tmp) / "worktrees"
            )
            self.assertIsNotNone(release, created)
            assert release is not None
            candidate_app = pathlib.Path(release.app_worktree_path)
            (candidate_app / "src" / "collision").write_text("candidate file\n", encoding="utf-8")
            release, committed = commit_candidate(release, "candidate")
            self.assertTrue(committed["ok"], committed)

            main_collision = app / "src" / "collision"
            main_collision.mkdir()
            (main_collision / "main.py").write_text("MAIN = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "main directory"], cwd=root, check=True, capture_output=True, text=True)
            main_before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()

            release, promotion = promote_candidate(release, app)

            self.assertFalse(promotion["ok"])
            self.assertEqual(promotion["reason"], "promotion_cherry_pick_failed")
            self.assertEqual(promotion["cherry_pick_abort"]["returncode"], 0)
            self.assertEqual(run_git(["status", "--porcelain"], root)["stdout"], "")
            main_after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(main_before, main_after)
            self.assertTrue(pathlib.Path(release.worktree_path).exists())
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)

    def test_overlapping_cherry_pick_requires_repair_without_destroying_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self._repo(tmp)
            release, created = create_candidate_worktree(app, "proposal:overlap", base_dir=pathlib.Path(tmp) / "worktrees")
            self.assertIsNotNone(release, created)
            assert release is not None
            candidate_app = pathlib.Path(release.app_worktree_path)
            (candidate_app / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")
            release, committed = commit_candidate(release, "candidate")
            self.assertTrue(committed["ok"], committed)

            (app / "src" / "demo.py").write_text("VALUE = 3\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "main overlap"], cwd=root, check=True, capture_output=True, text=True)
            main_before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()

            release, promotion = promote_candidate(release, app)

            self.assertFalse(promotion["ok"])
            self.assertEqual(promotion["reason"], "promotion_overlap_requires_repair")
            self.assertEqual(release.status, "promotion_overlap_requires_repair")
            self.assertEqual(promotion["overlapping_paths"], ["agentic_trading_swarm_mvp/src/demo.py"])
            main_after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(main_before, main_after)
            self.assertTrue(pathlib.Path(release.worktree_path).exists())
            self.assertEqual(run_git(["status", "--porcelain"], root)["stdout"], "")
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)

    def test_deferred_champion_update_does_not_move_latest_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, app = self._repo(tmp)
            release, created = create_candidate_worktree(app, "proposal:deferred-champion", base_dir=pathlib.Path(tmp) / "worktrees")
            self.assertIsNotNone(release, created)
            assert release is not None
            (pathlib.Path(release.app_worktree_path) / "src" / "demo.py").write_text("VALUE = 2\n", encoding="utf-8")
            release, committed = commit_candidate(release, "candidate")
            self.assertTrue(committed["ok"], committed)

            release, promotion = promote_candidate(release, app, update_champion=False)

            self.assertTrue(promotion["ok"], promotion)
            self.assertTrue(promotion["champion_update_deferred"])
            self.assertEqual(promotion["champion"]["status"], "deferred_by_policy")
            self.assertNotEqual(run_git(["rev-parse", "--verify", "champion/latest"], root)["returncode"], 0)
            champion_update = update_champion_latest(root, promotion["promoted_commit"])
            self.assertTrue(champion_update["ok"], champion_update)
            self.assertEqual(
                run_git(["rev-parse", "champion/latest"], root)["stdout"].strip(),
                promotion["promoted_commit"],
            )
            cleanup = cleanup_worktree(release, app)
            self.assertTrue(cleanup["ok"], cleanup)


if __name__ == "__main__":
    unittest.main()
