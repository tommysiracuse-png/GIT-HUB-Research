"""Shared contracts for autonomous code-evolution releases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


LEGACY_SUCCESS_STATUSES = {"merged_probation", "kept", "workspace_applied_probation", "workspace_kept"}

CANDIDATE_STATUSES = {
    "proposed",
    "triaged",
    "implementing",
    "implementation_failed",
    "sandbox_passed",
    "candidate_committed",
    "canary_running",
    "promoted",
    "reverted",
    "archived_failed",
    "superseded",
}


@dataclass
class CandidateRelease:
    proposal_id: str
    parent_commit: str
    branch_name: str
    worktree_path: str
    app_worktree_path: str
    candidate_commit: str | None = None
    status: str = "implementing"
    tests: dict[str, Any] = field(default_factory=dict)
    canary: dict[str, Any] = field(default_factory=dict)
    promotion_reason: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "parent_commit": self.parent_commit,
            "branch_name": self.branch_name,
            "worktree_path": self.worktree_path,
            "app_worktree_path": self.app_worktree_path,
            "candidate_commit": self.candidate_commit,
            "status": self.status,
            "tests": self.tests,
            "canary": self.canary,
            "promotion_reason": self.promotion_reason,
        }


def release_metadata_from_row(row: dict[str, Any]) -> dict[str, Any]:
    evaluation = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else {}
    metadata = evaluation.get("release") if isinstance(evaluation.get("release"), dict) else {}
    return metadata
