"""user treeへ触れない専有git worktree管理。"""

from __future__ import annotations

import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orc.errors import WorktreeError
from orc.paths import StatePaths, ensure_private_dir, validate_identifier

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Worktree:
    """Managerが所有するtask worktree。"""

    task_id: str
    base_commit: str
    path: Path


class WorktreeManager:
    """worktree addと一時probeだけを許可する。"""

    def __init__(self, repo_path: Path, run_id: str) -> None:
        self.repo_path = repo_path.resolve(strict=True)
        self.paths = StatePaths.for_run(self.repo_path, run_id)
        state_root = self.paths.root.resolve()
        if state_root == self.repo_path or state_root.is_relative_to(self.repo_path):
            raise WorktreeError("state area must be outside the user repository")
        ensure_private_dir(state_root)
        self.worktree_root = self.paths.worktree_root

    def create(self, task_id: str, base_commit: str) -> Worktree:
        """指定commitのdetached worktreeをstate領域へ作る。"""
        try:
            safe_task_id = validate_identifier(task_id, label="task_id")
        except ValueError as error:
            raise WorktreeError(str(error)) from error
        self._validate_commit(base_commit)
        ensure_private_dir(self.worktree_root)
        target = (self.worktree_root / safe_task_id).resolve(strict=False)
        if not target.is_relative_to(self.worktree_root.resolve()):
            raise WorktreeError(f"worktree target outside state area: {target}")
        if target.exists():
            raise WorktreeError(f"worktree already exists: {safe_task_id}")
        self._git("worktree", "add", "--detach", str(target), base_commit)
        target.chmod(0o700)
        return Worktree(safe_task_id, base_commit, target)

    def probe(self, base_commit: str) -> bool:
        """一時worktreeを実際に作成・削除して利用可否を確かめる。"""
        task_id = f"probe-{secrets.token_hex(8)}"
        worktree = self.create(task_id, base_commit)
        try:
            marker = worktree.path / ".orc-probe"
            marker.write_text("probe\n", encoding="utf-8")
            marker.unlink()
        finally:
            self._remove_probe(worktree)
        return True

    def create_integration(self, base_commit: str) -> tuple[Worktree, str]:
        """Create one non-overwriting `orc/<run_id>` branch in its dedicated worktree."""
        self._validate_commit(base_commit)
        branch = f"orc/{self.paths.run_id}"
        ref = f"refs/heads/{branch}"
        existing = subprocess.run(
            ["git", "-C", str(self.repo_path), "show-ref", "--verify", "--quiet", ref],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if existing.returncode == 0:
            raise WorktreeError(f"integration branch already exists: {branch}")
        if existing.returncode not in {0, 1}:
            raise WorktreeError("integration branch existence check failed")
        ensure_private_dir(self.worktree_root)
        target = (self.worktree_root / "_integration").resolve(strict=False)
        if target.exists():
            raise WorktreeError("integration worktree already exists")
        self._git("worktree", "add", "-b", branch, str(target), base_commit)
        target.chmod(0o700)
        return Worktree("_integration", base_commit, target), branch

    def _remove_probe(self, worktree: Worktree) -> None:
        if not worktree.task_id.startswith("probe-"):
            raise WorktreeError("only temporary probe worktrees may be removed")
        expected = (self.worktree_root / worktree.task_id).resolve(strict=False)
        if worktree.path != expected or not expected.is_relative_to(self.worktree_root.resolve()):
            raise WorktreeError("probe worktree path mismatch")
        self._git("worktree", "remove", "--force", str(expected))

    def _validate_commit(self, base_commit: str) -> None:
        if not COMMIT_PATTERN.fullmatch(base_commit):
            raise WorktreeError("base_commit must be a full lowercase SHA-1")
        self._git("cat-file", "-e", f"{base_commit}^{{commit}}")

    def _git(self, *args: str) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise WorktreeError(f"git worktree command failed: {message}")
