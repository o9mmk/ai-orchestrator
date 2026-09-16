"""Explicit, narrow, terminal-run garbage collection."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orc.errors import OrchestratorError
from orc.lease import LeaseManager
from orc.paths import StatePaths
from orc.run_snapshot import load_run_snapshot


class GcRefused(OrchestratorError):
    """The exact run cannot be safely and explicitly deleted."""


@dataclass(frozen=True)
class GcOutcome:
    """Irreversible deletion evidence safe to display."""

    run_id: str
    run_deleted: bool
    worktrees_deleted: int
    branch_retained: str | None


class GcService:
    """Delete only one confirmed terminal run; never delete its candidate branch."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve(strict=True)

    def collect(
        self,
        run_id: str,
        *,
        confirm_run_id: str,
        force_unmerged: bool = False,
    ) -> GcOutcome:
        """Validate exact targets and explicit confirmation before any deletion."""
        if confirm_run_id != run_id:
            raise GcRefused("confirmation must exactly match run_id")
        snapshot = load_run_snapshot(self.repo_path, run_id)
        state = snapshot.manifest["state"]
        if state not in {"REFUSED", "FAILED", "HALTED", "CANCELLED", "COMPLETED"}:
            raise GcRefused(f"run is not terminal: {state}")
        lease_manager = LeaseManager(self.repo_path)
        if lease_manager.lease_path.exists():
            raise GcRefused("repo lease exists; refusing gc")
        branch = f"orc/{run_id}"
        branch_exists = _branch_exists(self.repo_path, branch)
        if branch_exists and state != "COMPLETED" and not force_unmerged:
            raise GcRefused("unapproved integration branch requires --force-unmerged")
        owned_root = StatePaths.for_run(self.repo_path, run_id).worktree_root
        removed = self._remove_worktrees(owned_root)
        run_dir = snapshot.run_dir.resolve(strict=True)
        expected = StatePaths.for_run(self.repo_path, run_id).run_dir.resolve(strict=True)
        if run_dir != expected or run_dir.is_symlink():
            raise GcRefused("run deletion target mismatch")
        shutil.rmtree(run_dir)
        return GcOutcome(run_id, True, removed, branch if branch_exists else None)

    def _remove_worktrees(self, root: Path) -> int:
        if not root.exists():
            return 0
        if root.is_symlink() or root.resolve(strict=True) != root:
            raise GcRefused("worktree deletion target mismatch")
        all_entries = sorted(root.iterdir())
        if any(not path.is_dir() or path.is_symlink() for path in all_entries):
            raise GcRefused("unexpected entry in owned worktree root")
        entries = all_entries
        for path in entries:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise GcRefused("worktree escaped owned root")
            result = subprocess.run(
                ["git", "-C", str(self.repo_path), "worktree", "remove", "--force", str(resolved)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise GcRefused("git refused owned worktree removal")
        if root.exists():
            root.rmdir()
        return len(entries)


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode not in {0, 1}:
        raise GcRefused("integration branch check failed")
    return result.returncode == 0
