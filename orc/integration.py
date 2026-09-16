"""M8 isolated integration candidate branch preparation."""

from __future__ import annotations

import hashlib
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orc.errors import IntegrationError, WorktreeError
from orc.git_facts import collect_git_facts
from orc.paths import StatePaths, validate_identifier
from orc.store import RunStateStore
from orc.worktree import WorktreeManager


@dataclass(frozen=True)
class IntegrationOutcome:
    """Human-facing candidate ref without automatic merge."""

    state: str
    reason: str
    branch: str | None
    commit: str | None
    merge_command: str | None


class IntegrationService:
    """Apply verified DONE patches only inside an `orc/<run_id>` worktree."""

    def __init__(self, store: RunStateStore) -> None:
        self.store = store
        self.repo_path = store.repo_path

    def prepare(self) -> IntegrationOutcome:
        """Build a candidate branch, or stop before branch creation on stale HEAD."""
        manifest = self.store.read_manifest()
        if manifest["state"] != "INTEGRATING":
            raise IntegrationError("integration requires INTEGRATING state")
        checkpoint = self.store.verify_integrity()
        before = _user_tree_snapshot(self.repo_path)
        if collect_git_facts(self.repo_path).head != manifest["base_commit"]:
            state = self.store.transition("stale_head", reason="stale_head")
            return IntegrationOutcome(state.value, "stale_head", None, None, None)
        try:
            worktree, branch = WorktreeManager(
                self.repo_path, self.store.run_id
            ).create_integration(manifest["base_commit"])
        except WorktreeError as error:
            raise IntegrationError(str(error)) from error
        patches = _done_patches(self.store, checkpoint.tasks)
        for patch in patches:
            _git_checked(worktree.path, "apply", "--check", str(patch))
            _git_checked(worktree.path, "apply", "--index", str(patch))
        staged = subprocess.run(
            ["git", "-C", str(worktree.path), "diff", "--cached", "--quiet"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if staged.returncode not in {0, 1}:
            raise IntegrationError("integration staged diff check failed")
        if staged.returncode == 1:
            _git_checked(
                worktree.path,
                "-c",
                "user.name=orc",
                "-c",
                "user.email=orc@example.invalid",
                "commit",
                "-m",
                f"feat: orc integration candidate {self.store.run_id}",
            )
        commit = _git_checked(worktree.path, "rev-parse", "HEAD")
        after = _user_tree_snapshot(self.repo_path)
        if after != before:
            raise IntegrationError("user worktree changed during integration")
        merge_command = f"git merge --no-ff {branch}"
        self.store.append_event(
            "integration_candidate_ready",
            "manager",
            {
                "branch": branch,
                "commit": commit,
                "patch_count": len(patches),
                "merge_command": merge_command,
            },
        )
        state = self.store.transition("integration_ready", reason="integration_ready")
        return IntegrationOutcome(
            state.value,
            "integration_ready",
            branch,
            commit,
            merge_command,
        )


def _done_patches(store: RunStateStore, tasks: dict[str, Any]) -> tuple[Path, ...]:
    patches: list[Path] = []
    for task_id, task in sorted(tasks.items()):
        safe_task = validate_identifier(task_id, label="task_id")
        if task["state"] != "DONE":
            continue
        attempt = task["attempt"]
        path = store.run_dir / "tasks" / safe_task / f"attempt-{attempt}" / "patch.diff"
        if not path.exists() and isinstance(task.get("reused_from"), str):
            path = _reused_patch(store, safe_task, task)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise IntegrationError("integration patch type is unsafe")
            patches.append(path)
    return tuple(patches)


def _reused_patch(store: RunStateStore, task_id: str, task: dict[str, Any]) -> Path:
    source_run = validate_identifier(task["reused_from"], label="reused_from")
    source_dir = StatePaths.for_run(store.repo_path, source_run).run_dir
    references = task.get("reused_artifacts")
    if not isinstance(references, list):
        raise IntegrationError("reused task artifact references are missing")
    suffix = f"tasks/{task_id}/attempt-{task['attempt']}/patch.diff"
    matches = [item for item in references if isinstance(item, dict) and item.get("path") == suffix]
    if len(matches) != 1 or not isinstance(matches[0].get("digest"), str):
        raise IntegrationError("reused task patch reference is invalid")
    path = source_dir / suffix
    try:
        info = path.lstat()
        payload = path.read_bytes()
    except OSError as error:
        raise IntegrationError("reused task patch is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or hashlib.sha256(payload).hexdigest() != matches[0]["digest"]
    ):
        raise IntegrationError("reused task patch digest mismatch")
    return path


def _git_checked(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise IntegrationError(f"integration git command failed: {args[0]}")
    return result.stdout.strip()


def _user_tree_snapshot(repo: Path) -> tuple[str, str, bytes]:
    facts = collect_git_facts(repo)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return facts.head, facts.branch, status
