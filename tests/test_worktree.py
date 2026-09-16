"""設計書§8.1 worktree隔離のテスト。"""

import subprocess
from pathlib import Path

import pytest

from orc.errors import WorktreeError
from orc.worktree import WorktreeManager


def git(repo: Path, *args: str) -> str:
    """fixture repoでgit commandを実行する。"""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def committed_repo(repo: Path) -> Path:
    """tracked file 1件のgit repoを返す。"""
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "orc-test")
    git(repo, "config", "user.email", "orc-test@example.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "initial")
    return repo


def test_create_uses_state_area_without_changing_user_tree(committed_repo: Path) -> None:
    """専有worktreeをORC_STATE_DIR配下に作り、user treeをbyte不変に保つ。"""
    before_bytes = (committed_repo / "app.py").read_bytes()
    before_status = git(committed_repo, "status", "--porcelain")
    base_commit = git(committed_repo, "rev-parse", "HEAD")
    manager = WorktreeManager(committed_repo, "run-1")

    worktree = manager.create("task-1", base_commit)

    assert (worktree.path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (committed_repo / "app.py").read_bytes() == before_bytes
    assert git(committed_repo, "status", "--porcelain") == before_status
    assert "worktrees" in worktree.path.parts


def test_probe_actually_creates_and_removes_temporary_worktree(committed_repo: Path) -> None:
    """Stage 2 probeはworktree add/removeを実行し残骸を残さない。"""
    manager = WorktreeManager(committed_repo, "run-1")
    base_commit = git(committed_repo, "rev-parse", "HEAD")

    result = manager.probe(base_commit)

    assert result is True
    assert list(manager.worktree_root.glob("probe-*")) == []


@pytest.mark.parametrize("task_id", ["../escape", "/absolute", "bad/name"])
def test_unsafe_task_id_is_rejected(committed_repo: Path, task_id: str) -> None:
    """worktree targetをstate領域外へ逸脱させるidentifierを拒否する。"""
    manager = WorktreeManager(committed_repo, "run-1")
    base_commit = git(committed_repo, "rev-parse", "HEAD")

    with pytest.raises(WorktreeError, match="invalid task_id"):
        manager.create(task_id, base_commit)


def test_state_area_inside_user_repo_is_rejected(
    committed_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """環境変数でstate領域をrepo内へ寄せても隔離を破らない。"""
    monkeypatch.setenv("ORC_STATE_DIR", str(committed_repo / ".orc-state"))

    with pytest.raises(WorktreeError, match="outside the user repository"):
        WorktreeManager(committed_repo, "run-1")
