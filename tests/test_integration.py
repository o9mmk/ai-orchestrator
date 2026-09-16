"""M8 isolated integration branch and stale HEAD tests."""

from pathlib import Path

import pytest

from orc.errors import IntegrationError
from orc.integration import IntegrationService
from tests.m8_helpers import git, make_committed_repo, make_running_store

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _integrating_store(repo: Path):  # type: ignore[no-untyped-def]
    store = make_running_store(repo)
    store.record_task_state(
        "task-1", {"state": "DONE", "attempt": 1, "review_cycles": 0}
    )
    patch = store.run_dir / "tasks/task-1/attempt-1/patch.diff"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(PATCH, encoding="utf-8")
    store.write_checkpoint(run_state="RUNNING")
    store.transition("tasks_done")
    return store


def test_integration_branch_applies_done_patch_without_touching_user_tree(
    tmp_path: Path,
) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    store = _integrating_store(repo)
    before_bytes = (repo / "app.py").read_bytes()
    before_status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    outcome = IntegrationService(store).prepare()

    assert outcome.state == "AWAITING_APPROVAL"
    assert outcome.branch == "orc/run-1"
    assert outcome.merge_command == "git merge --no-ff orc/run-1"
    assert git(repo, "show", "orc/run-1:app.py") == "VALUE = 2"
    assert (repo / "app.py").read_bytes() == before_bytes
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert git(repo, "branch", "--show-current") == "main"


def test_stale_head_stops_before_branch_or_worktree_creation(tmp_path: Path) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    store = _integrating_store(repo)
    (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "advance head")

    outcome = IntegrationService(store).prepare()

    assert outcome.state == "AWAITING_APPROVAL"
    assert outcome.reason == "stale_head"
    assert outcome.branch is None
    assert git(repo, "branch", "--list", "orc/run-1") == ""
    assert (store.paths.worktree_root / "_integration").exists() is False


def test_existing_integration_branch_is_not_force_updated(tmp_path: Path) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    store = _integrating_store(repo)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "orc/run-1", base)

    with pytest.raises(IntegrationError, match="already exists"):
        IntegrationService(store).prepare()

    assert git(repo, "rev-parse", "orc/run-1") == base
