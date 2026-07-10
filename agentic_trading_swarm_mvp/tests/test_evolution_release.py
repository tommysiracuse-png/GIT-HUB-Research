from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.worktree import (  # noqa: E402
    cleanup_worktree,
    commit_candidate,
    create_candidate_worktree,
    release_preflight,
    run_git,
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


if __name__ == "__main__":
    unittest.main()
