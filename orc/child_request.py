"""Dedicated-worktree and role validation before child spawn."""

from __future__ import annotations

from pathlib import Path

from orc.errors import ChildExecutionError
from orc.store import RunStateStore
from orc.worktree import Worktree

ROLE_TIMEOUT_KEYS = {
    "researcher": "research",
    "implementer": "implement",
    "reviewer": "review",
}


def validate_child_request(
    store: RunStateStore,
    worktree: Worktree,
    *,
    task_id: str,
    role: str,
    attempt: int,
    prompt: str,
) -> Path:
    """Return the owned cwd only when every spawn boundary matches."""
    root = validate_owned_worktree(store, worktree, task_id=task_id)
    if role not in ROLE_TIMEOUT_KEYS:
        raise ChildExecutionError(f"unsupported child role: {role}")
    if attempt < 1 or not prompt:
        raise ChildExecutionError("attempt and prompt must be non-empty")
    return root


def validate_owned_worktree(
    store: RunStateStore,
    worktree: Worktree,
    *,
    task_id: str,
) -> Path:
    """Validate the Manager-owned cwd independently from a child role."""
    if task_id != worktree.task_id:
        raise ChildExecutionError("task_id does not match dedicated worktree")
    if worktree.path.is_symlink():
        raise ChildExecutionError("dedicated worktree root must not be a symlink")
    root = worktree.path.resolve(strict=True)
    expected = (store.paths.worktree_root / task_id).resolve(strict=False)
    if root != expected or not root.is_relative_to(store.paths.worktree_root.resolve()):
        raise ChildExecutionError("child cwd is not the state-owned dedicated worktree")
    if not (root / ".git").exists():
        raise ChildExecutionError("dedicated worktree is missing .git metadata")
    return root
